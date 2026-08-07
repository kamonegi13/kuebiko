"""LLM 出力スキーマ + summary 関連の純粋ヘルパ (src.main から分割)。

- ``SummaryOutput`` (summarizer.j2 の JSON 構造)
- 日本語判定 / IOC self-URL 除去 / importance cap / event_date 正規化。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.cti.llm_routing_flags import EditorialStance
from src.logging_config import get_logger
from src.tools.discord_publisher import Importance

_log = get_logger(__name__)

# ---------- 定数 ----------

# C1 (チャンネル config-driven 化): Literal 6 固定 → str に緩和。
# チャンネル定義 (有効集合 / fallback / order) の SSoT は src/tools/channel_registry.py。
DiscordChannel = str


def _is_mostly_japanese(text: str) -> bool:
    """**deprecated** Phase 5J-2 で ``_has_japanese_kana`` に置換済。

    旧仕様: ASCII 比率 < 50% で「日本語主体」と判定。
    問題: 中国語 (CJK 漢字) は ASCII 0% なので日本語と誤判定 → 翻訳スキップ。
    """
    if not text:
        return False
    ascii_count = sum(1 for c in text if ord(c) < 128)
    return ascii_count / len(text) < 0.5


# Phase 5J-2: 日本語特異性の判定 (中国語と区別)。
# ひらがな (U+3040-309F) / 全角カタカナ (U+30A0-30FF) / 半角カナ (U+FF66-FF9F) の存在で判定。
# 漢字のみのタイトルは「ほぼ日本語」とは判定しないが、LLM 翻訳に委ねれば問題ない。
_JAPANESE_KANA_RE = re.compile(r"[぀-ゟ゠-ヿｦ-ﾟ]")


def _has_japanese_kana(text: str) -> bool:
    """日本語特有のひらがな/カタカナを含むかどうか。

    LLM 翻訳のスキップ判定に使う。中国語 (CJK 漢字のみ) は False を返し、
    LLM 翻訳経路を通すことで「重要数据性质の再认识」のような中国語タイトルも
    日本語化される。
    """
    if not text:
        return False
    return bool(_JAPANESE_KANA_RE.search(text))


# ---------- 和訳タイトルの接地検証 (監査 2026-08-01) ----------
# summarizer が本文と無関係な「ありがちな CTI 見出し」を幻覚するケースの決定的ガード
# (The Register の無関係 3 記事が同一見出し「ロシア、Signal 偽装フィッシング」で
# alert 3 連投)。日英翻訳のため語一致では接地を検証できないが、幻覚見出しの特徴は
# **原文に存在しない固有名詞 (英字トークン)** を含むこと。カタカナ固有名詞は
# 日英対応が検証不能のため対象外 (fail-open — 誤 fallback より見逃しを許容)。
_TITLE_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
# 幻覚の証拠にならない一般語 (LLM が体裁で足しても正当なもの)
_TITLE_GENERIC_LATIN: frozenset[str] = frozenset(
    {"web", "api", "url", "app", "apps", "online", "internet", "and", "the", "for", "via", "new"}
)


def ungrounded_title_tokens(title_ja: str, source_text: str) -> list[str]:
    """生成見出し中の英字トークンのうち、原文 (原題+本文) に無いものを返す。

    非空リスト = 幻覚見出しの疑い (呼び出し側で原題 fallback + 警告)。
    空リスト = 接地確認 or 判定材料なし (英字トークンが無い) の fail-open。
    """
    if not title_ja:
        return []
    src = (source_text or "").lower()
    out: list[str] = []
    for m in _TITLE_LATIN_TOKEN_RE.finditer(title_ja):
        tok = m.group(0).lower()
        if tok in _TITLE_GENERIC_LATIN:
            continue
        if tok not in src:
            out.append(tok)
    return out


def _drop_self_url(iocs: list[str], self_url: str) -> list[str]:
    """記事自身の URL ホストと一致する URL/domain を IOC から除外する (Phase 5J-2)。

    LLM が summarizer prompt 中の article.url や本文中の参照リンクを
    IOC として返してしまうケースの構造的防御。filter_benign の後の最終層。
    """
    if not iocs or not self_url:
        return list(iocs)
    from urllib.parse import urlparse

    try:
        host = urlparse(self_url).netloc.lower()
    except (ValueError, AttributeError):
        return list(iocs)
    if not host:
        return list(iocs)
    # `www.` プレフィクスを正規化して、bare domain と www 付きを統一比較
    host_bare = host.removeprefix("www.")
    out: list[str] = []
    for ioc in iocs:
        if not ioc:
            continue
        ioc_lc = ioc.lower()
        # URL: スキーマ込みで host (www 込みと bare の両方) が含まれていれば除外
        if ioc_lc.startswith(("http://", "https://")) and (host in ioc_lc or host_bare in ioc_lc):
            continue
        # ドメイン: bare/www 双方を比較。完全一致 or 「.bare」末尾一致 (sub.host.com) を除外
        if "." in ioc_lc and not ioc_lc.startswith(("http://", "https://")):
            if ioc_lc in (host, host_bare):
                continue
            if ioc_lc.endswith("." + host_bare):
                continue
        out.append(ioc)
    return out


# ---------- LLM 出力スキーマ ----------


ArticleType = Literal[
    "breaking",  # 進行中インシデント / 速報
    "advisory",  # ベンダー Advisory / CVE 警告
    "recap",  # 週間 / 月間まとめ / Top N
    "tutorial",  # 解説 / 入門 / 列挙系
    "research",  # 技術論文 / 詳細分析
    "press",  # 製品発表 / 企業ニュース
    "opinion",  # 論説 / コラム
]


class SummaryOutput(BaseModel):
    """``prompts/summarizer.j2`` で LLM に生成させる JSON 構造。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    # Phase 5J-2: title_ja を required に格上げ (MoE 系で optional 省略を防ぐ)。
    title_ja: str = Field(min_length=1, max_length=120)
    # Phase 5T-O: Inoreader 経路では BLUF が title と重複するため LLM 生成を廃止。
    # field は後方互換のため optional として残す (Grok 経路の機械的 BLUF 生成では引き続き使用)。
    bluf: str = ""
    importance: Importance
    category: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    iocs: list[str] = Field(default_factory=list)
    mitre_techniques: list[str] = Field(default_factory=list)
    # Phase Diamond: Capability 軸の構造化フィールド。本文に明示された固有名詞のみ。
    malware_families: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    analyst_note: str | None = None
    # Phase 5K: 記事タイプ (recap / tutorial 等を routing で識別するため)。
    # required にすることで LLM に確実に生成させる。判定不能なら "breaking" を返す指示。
    article_type: ArticleType = "breaking"
    # editorial_stance: 編集スタンス (factual_report/analytical/opinion/propaganda/unknown)。
    # **summarizer は出力しない** (過負荷で unknown 連発のため prompt から除去)。enrichment が
    # focused 分類器で確定し model_copy で上書き。本フィールドは上書き先 (既定 unknown)。
    editorial_stance: EditorialStance = "unknown"
    # Phase 5L-3: ルーティング判定用の構造化フラグ。LLM が欠落させた / 不正値を
    # 返した場合は parse_routing_flags() で defensive default に倒す。
    routing_flags: dict[str, object] = Field(default_factory=dict)
    # Phase H: PMESII-PT 軸 (multi-label)。LLM 欠落時は normalizer が
    # feed/category default で union 補完する。
    pmesii_axes: list[str] = Field(default_factory=list)
    # Phase H: Diamond Model victim vertex (raw text、normalizer で canonical 化)。
    # 未知値は articles.victim_*_canonical='uncategorized' + raw を保存して
    # weekly-taxonomy-review で LLM 提案 → user 承認で yaml 拡充。
    victim_sector: str | None = None
    victim_country: str | None = None
    # #7 (geo-map 精密化): 被害組織の所在都市/地域 (明示時のみ、原文ママ)。都市レベル plot 用。
    # 推測補完しない (国だけ分かれば null)。entity_type='victim_city' で永続化。
    victim_city: str | None = None
    # 地政学情勢ブリッジ (2026-06-22): geopolitical/policy 事象の **関与国 (主体国の集合)**。
    # victim_country (被害国・サイバー用) と別に、地政学事象の当事国 (中×台 → ["China","Taiwan"])
    # を持つ。entity_type='involved_country' に ISO 正規化して永続化し、actor.nation を介した
    # サイバー↔地政学の相関 (情勢把握) の join key にする。サイバー事案では空配列。
    involved_countries: list[str] = Field(default_factory=list)
    # Phase 2 (geo-map): 地図プロット用の **被害組織名**。「実際に攻撃を受け侵害された
    # 組織/企業/団体」のみ (原文ママの固有名詞)。ベンダ/ソフトメーカー (脆弱性提供元)・
    # 悪用された製品・攻撃者・研究者/報告者は **含めない**。攻撃された組織が記事に明記
    # されない場合は空 list (ベンダ等で代替しない=偽の点を出さない)。複数被害 (リーク
    # サイト列挙等) に対応するため list。
    victim_orgs: list[str] = Field(default_factory=list)
    # Phase Diamond-Axes: Diamond Model の 2 meta-feature 軸 (socio_political / technical)。
    # 2026-07-13 から summarizer は出力しない (過負荷で末尾フィールドが枯死したため
    # prompt から除去)。analysis_axes_classifier (focused) が model_copy で上書きする。
    # 欠落 / 不正値は parse_diamond_axes が unknown / 空に倒す (後方互換)。
    diamond: dict[str, object] = Field(default_factory=dict)
    # 時間軸レイヤ b/c (2026-06-27): 取得/報道時刻と「実発生時刻」を分離する。
    # 2026-07-13 から summarizer は出力しない — analysis_axes_classifier が上書きする
    # (basis=reported で報道日係留を許可する「確度つき記録」レジーム)。
    # event_date = 主要事象の発生(または検知/公表)日 (YYYY-MM-DD、月のみ明示は当月1日)。
    # event_date_basis = occurred|detected|disclosed|reported|relative (date が何を指すか=正直さ)。
    # compromise_date = 初期侵害開始日 (明示時のみ)。dwell = event_date - compromise_date。
    event_date: str | None = None
    event_date_basis: str | None = None
    compromise_date: str | None = None
    # P4 (tagging survey CoA gap): 本文に明示された対処 (パッチ/回避策/緩和策) の 1 文要約。
    # 「読んで何をするか」の行動可能性軸。本文に無ければ null (一般論を創作しない)。
    remediation: str | None = None


# ---------- importance 決定的ガード (Phase B-cal2, 2026-06-04) ----------

# vulnerability/advisory の importance は LLM (高速 26B) が一律 high に過大評価する
# 傾向がある (実データ監査で vuln 82% high / low 0%、パッチ済み CVE まで high)。
# summarizer.j2 の基準では high = KEV 級 / 実悪用中 / 0day のみ。悪用シグナルが
# title+summary+本文に無い high は medium に降格する決定的ガード。
# PoC 公開のみ・パッチ済み・未悪用は high に値しない (medium 以下が正)。
# 監査 2026-07-05 P1: 悪用の**事実断定形**のみ通す。旧 regex は接尾グループ全 optional
# (裸の「悪用」) と裸 `exploited` により JVN/IPA 定型の仮定文「悪用された場合…」や
# "could be exploited" で発火し、cap が実質無効化していた (high 汚染 Tier3#7 の正体)。
# 語彙で拾えない確定悪用は KEV カタログ照合 (下) が回収するため、regex は精度優先でよい。
_EXPLOIT_HIGH_SIGNAL = re.compile(
    r"actively\s+exploit\w*|exploit(?:ed|ation)\s+in\s+(?:the\s+)?(?:wild|attacks)|"
    r"in[- ]the[- ]wild\s+exploit\w*|(?:has|have)\s+been\s+exploited|"
    r"being\s+(?:actively\s+)?exploited|exploitation\s+(?:has\s+been\s+|was\s+)?"
    r"(?:observed|detected|confirmed)|under\s+(?:active\s+)?(?:attack|exploitation)|"
    r"known\s+exploited|\bKEV\b|zero[- ]?day|(?<!\d)0[- ]?day|ゼロデイ|"
    r"悪用(?:中|が確認|を確認|が観測|を観測|の事実|されて(?:いる|おり))|"
    r"実際に(?:攻撃|悪用)|積極的に悪用|広く悪用|緊急(?:対応|警告)",
    re.IGNORECASE,
)
# 固有名詞は悪用シグナルではないため照合前に除去 (「Zero Day Initiative」「ZDI-26-xxx」誤発火防止)
_EXPLOIT_PRE_REMOVE = re.compile(r"zero[- ]?day\s+initiative|\bZDI(?:-\d{2}-\d+)?\b", re.IGNORECASE)
_CAP_CVE_RE = re.compile(r"CVE-(?:19|20)\d{2}-\d{4,7}", re.IGNORECASE)
# このカテゴリのみ guard 対象 (apt/malware/breach/incident は別軸で actionability 判定)
_VULN_CAP_CATEGORIES = frozenset({"vulnerability", "advisory"})


def _text_has_kev_cve(text: str) -> bool:
    """text 中の CVE のいずれかが CISA KEV catalog に載っているか (fail-safe False)。"""
    try:
        from src.tools import kev_client

        cves = [m.group(0).upper() for m in _CAP_CVE_RE.finditer(text)]
        return bool(cves) and bool(kev_client.any_cve_on_kev(cves))
    except Exception as e:  # noqa: BLE001 — KEV cache 不調でも cap 自体は止めない
        _log.warning("kev_check_failed_in_cap", error=str(e))
        return False


def _cap_vuln_importance(importance: Importance, category: str, *text_parts: str) -> Importance:
    """vulnerability/advisory の high を悪用シグナル不在なら medium に降格する。

    high のまま残すのは (1) KEV 掲載 CVE を含む (決定論・語彙非依存)、または
    (2) 実悪用の事実断定 / 0day / 緊急 の語が title・summary・本文に現れる場合のみ。
    """
    if importance != "high" or category not in _VULN_CAP_CATEGORIES:
        return importance
    haystack = " ".join(p for p in text_parts if p)
    # KEV カタログ照合が最優先: 本文に魔法語が無い KEV 記事の medium 降格 (Recall の穴) を防ぐ
    if _text_has_kev_cve(haystack):
        return importance
    if _EXPLOIT_HIGH_SIGNAL.search(_EXPLOIT_PRE_REMOVE.sub(" ", haystack)):
        return importance
    return "medium"


# ---------- 時間軸レイヤ b/c: event_date / dwell の検証 (2026-06-27) ----------

# 事象発生日が何を指すか (正直さ)。許可外は basis を捨てる。
_EVENT_DATE_BASES = frozenset({"occurred", "detected", "disclosed", "reported", "relative"})
# 月のみ (YYYY-MM) は当月 1 日に、年のみは却下 (粒度が荒すぎ event-time 分析に無価値)。
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$")
_EVENT_DATE_FLOOR = date(2000, 1, 1)  # これ以前は誤抽出とみなす


def _normalize_iso_date(raw: str | None, *, ceiling: date) -> str | None:
    """LLM 抽出の日付文字列を YYYY-MM-DD に正規化。実在 + 妥当域のみ通す。

    未来 (ceiling 超過) / 2000 以前 / 解析不能は None。月のみは当月 1 日に補う。
    偽の event_date は dwell / event-time 分析を汚すため保守的に弾く。
    """
    if not raw or not isinstance(raw, str):
        return None
    m = _ISO_DATE_RE.match(raw.strip())
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    day = int(m.group(3)) if m.group(3) else 1
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if d < _EVENT_DATE_FLOOR or d > ceiling:
        return None
    return d.isoformat()


def _normalize_temporal(
    event_date: str | None,
    basis: str | None,
    compromise_date: str | None,
    *,
    reference: date,
) -> tuple[str | None, str | None, str | None]:
    """event_date / basis / compromise_date を検証して返す (報道時刻と分離)。

    - event_date は reference (報道日 +1d 程度) を上限に: 事象は報道後に起きえない。
    - basis は許可 enum のみ、event_date が無ければ落とす。
    - compromise_date は event_date 以前のみ採用 (dwell が負になる抽出は捨てる)。
    """
    ev = _normalize_iso_date(event_date, ceiling=reference)
    b = basis if (ev and isinstance(basis, str) and basis in _EVENT_DATE_BASES) else None
    comp = _normalize_iso_date(compromise_date, ceiling=reference)
    # dwell の起点は侵害 → 検知/公表。event_date 以前でなければ意味を成さない。
    if comp and ev and comp > ev:
        comp = None
    elif comp and not ev:
        # event_date 不明だと dwell を測れないので compromise 単独は保持しない
        comp = None
    return ev, b, comp
