"""BriefingMessage → RoutingSignals 抽出 (Phase 5K, 5L-3 で LLM フラグ駆動化)。

ルーティング判定の入力となる客観的シグナルを集約する。Inoreader / Grok 両経路
から呼ばれる共通モジュール。

設計の階層 (Phase 5L-3 改修):
    - Primary 信号: prompts/briefing/summarizer.j2 で LLM が出した routing_flags
      (japan_targeted / is_breaking_critical / primary_actor_id / dedup_key /
      confidence)。LLM は本文全体を読んで文脈判断するため精度が高い。
    - Fallback 信号: 本ファイルで定義する regex (_JAPAN_GENERAL/CRITICAL_PATTERNS,
      _KEV_PATTERNS 等)。LLM confidence=low や routing_flags 欠落時のみ採用。

役割:
    - 日本標的の確度判定 (mentions_japan / mentions_japan_critical)
    - KEV / 0-day 検出
    - 既知 APT アクター検出
    - article_type 抽出 (article_classifier.detect_article_type を呼ぶ)
    - Grok の section_id / signal_type / corroboration を保持
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from src.cti.article_classifier import ArticleType, detect_article_type
from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.tools.discord_publisher import BriefingMessage

_log = get_logger(__name__)

# Grok JSONL 化 (2026-06-13) 以降、router を通るのは briefing のみ
SourceKind = Literal["briefing"]


# R0 (PIR target_channel override) の撤去 (2026-06-13) に伴い、routing 時の
# PIR 評価 (_compute_matched_pir_ids / matched_pir_ids signal) も撤去した。
# 記事×PIR の対応は post-hoc に src/pir/evaluator.py が計算する
# (daily focus / KPI / 情報フロー画面はそちらを使用)。

_CVE_TOKEN_RE = re.compile(r"CVE-(?:19|20)\d{2}-\d{4,7}", re.IGNORECASE)


def _cve_on_kev(msg: BriefingMessage, full_text: str) -> bool:
    """記事中の CVE のいずれかが CISA KEV catalog に載っているか (Phase 1 K5)。

    本文 prose の regex 推定ではなく CISA 公式 catalog 照合で deterministic に KEV を検出する。
    fetch 失敗 / catalog 空でも False に degrade し routing を壊さない (fail-safe)。
    """
    try:
        from src.tools.kev_client import any_cve_on_kev

        return bool(_article_cves(msg, full_text)) and any_cve_on_kev(
            list(_article_cves(msg, full_text))
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("kev_check_failed", error=str(e))
        return False


def _article_cves(msg: BriefingMessage, full_text: str) -> set[str]:
    """記事 (本文 + iocs) 中の CVE-ID 集合 (大文字)。"""
    cves = {m.group(0).upper() for m in _CVE_TOKEN_RE.finditer(full_text)}
    cves |= {str(i).upper() for i in (msg.iocs or []) if str(i).upper().startswith("CVE-")}
    return cves


def _max_cvss_for(msg: BriefingMessage, full_text: str) -> float:
    """記事中 CVE の NVD CVSS 最大値 (Phase 2.5 K5b、cache 参照のみ、fail-safe で 0.0)。"""
    try:
        from src.tools.nvd_client import max_cvss

        return max_cvss(list(_article_cves(msg, full_text)))
    except Exception as e:  # noqa: BLE001
        _log.warning("cvss_check_failed", error=str(e))
        return 0.0


@dataclass(frozen=True)
class RoutingSignals:
    """ルーティング判定の入力となる客観的シグナルの集合。

    Phase 5L-3: LLM が直接出力する routing_flags を primary シグナルとし、
    regex を fallback (LLM confidence=low 時の補助) にした。
    ``mentions_japan_critical`` 等の判定は両者の合議で決まる。
    """

    # 必須情報
    source: SourceKind
    importance: Literal["high", "medium", "low"]
    article_type: ArticleType

    # 日本関連 (LLM フラグ + regex の合議)
    mentions_japan: bool = False
    mentions_japan_critical: bool = False  # 日本企業/政府/防衛/重要インフラ
    is_japan_security_relevant: bool = False  # CTI 関連かつ日本

    # 脅威指標
    has_kev_or_active_exploit: bool = False
    is_zero_day: bool = False
    has_known_apt: bool = False
    # Phase 2.5 K5b: 記事中 CVE の NVD CVSS 最大値 (0.0=不明)。当面は観測用 signal
    # (router 判定には未注入、PIR shadow と同様に観察後に活性化)。
    max_cvss: float = 0.0

    # 内容性質
    is_security_relevant: bool = True  # category や本文から判定
    confidence_all_low: bool = False  # state_apt の信頼性 low only
    # Phase 5N: APT 関連組織の内部リーク (PIR 3) — i-Soon, NTC Vulkan 等
    is_apt_leak: bool = False

    # Phase 5L-3: LLM 構造化フラグ (primary 信号)
    llm_japan_targeted: bool = False
    llm_is_breaking_critical: bool = False
    llm_primary_actor_id: str = ""
    llm_dedup_key: str = ""
    llm_confidence: Literal["high", "medium", "low"] = "low"

    # Phase 5T-V: source-level quality signal
    # feed_title (Inoreader 経由のみセット、Grok は空)。R5b propaganda の reason ログ用。
    feed_title: str = ""
    # 直近 24h の brief 投稿件数 snapshot (R6 cap 判定用、0 なら cap 無効)
    brief_count_24h_snapshot: int = 0
    # BriefingMessage.category (R3.5 high_threat / R6 判定用、Grok 経路は空)
    category: str = ""
    # Phase B-R5b 改訂: LLM が判定した editorial stance。
    # values: factual_report / analytical / opinion / propaganda / unknown
    # propaganda 時は R5b で watch 降格。source 識別子から独立した content-based 判定。
    editorial_stance: str = "unknown"

    # 語彙拡張 ① (回収): 既に計算/抽出済だが routing 条件に未配線だった signal。
    # victim_sector = Diamond victim vertex の canonical sector (metadata 由来、空=未判定)。
    victim_sector: str = ""
    # threat_actor_nations = 検出アクターの sponsoring nation 集合 (cn/ru/kp/ir 等)。
    # actor 辞書 lookup の回収 (新検出なし)。「中露朝の脅威活動」を条件化するための語彙。
    threat_actor_nations: frozenset[str] = field(default_factory=frozenset)
    # 語彙拡張② マッチした user 定義 match_list の name 集合 (keyword_list 条件で参照)。
    matched_keyword_lists: frozenset[str] = field(default_factory=frozenset)

    # 元データ (デバッグ用)
    extras: dict[str, object] = field(default_factory=dict)


# Phase 5L-1: regex を 2 階層に分離。
# - GENERAL: 「日本に何らかの言及がある」レベルの弱信号 (mentions_japan 用)
# - CRITICAL: 「日本の重要組織が標的・被害として記載されている」強信号
# (is_japan_security_relevant / mentions_japan_critical 用)。
#
# 5L-1 で削除した汎用語:
#   - 「国内」: LLM 日本語要約で「国内外の組織」のように汎用使用される (誤検知の元凶)
#   - 「重要インフラ」: GENERAL から削除し CRITICAL のみで使用 (japanese phrase で誤反応)
#   - 「日本」単独 → 「日本語/日本食/日本酒」を除外する negative lookahead
#   - 「JAPAN」 → \b 単語境界を付与し JAPANESE 等のサブストリング誤検知を防止
_JAPAN_GENERAL_PATTERNS = re.compile(
    r"(日本(?!語|食|酒|料理|文化|海)|"
    r"日系|邦人|ジャパン|"
    r"\bJAPAN\b)",
    re.IGNORECASE,
)
# Phase 5O: 「Japan-targeted (被害当事者)」のみ判定する厳格化。
# 削除した語 (Phase 5O):
#   - JPCERT / NISC / 警察庁: 「発信者」として記事中に登場するだけで誤検知。
#     例: 「JPCERT/CC が ELECOM の脆弱性 advisory を公開」 → JPCERT は **発信者**、
#     しかし regex match で japan_watch 行きになる問題が観測 (5/7-8 53 件中ほぼ全件)
#   - NTT/KDDI/ソフトバンク/楽天: 「製品名」と「被害組織」を文字列で区別できない
#     例: 「楽天が侵害された」と「楽天モバイル製ルーター脆弱性」が同 regex で match
#   - トヨタ/ホンダ/日産/三菱/三井/住友: 同上
# 残した語 (これらは「攻撃事案」を強く示唆):
#   - 日本(政府|防衛|自衛隊)/日本企業/日本標的: 標的化を明示する用語
#   - 日系大手/国内重要インフラ: 攻撃対象表現
#   - 防衛省/防衛装備/自衛隊/JSDF: 国防機関 (発信者と被害が一致しやすい)
# 発信者・ベンダー識別は **LLM japan_targeted フラグ** に委譲し、regex は補助として
# 強い signal のみに絞る。
_JAPAN_CRITICAL_PATTERNS = re.compile(
    r"(日本(政府|防衛|自衛隊)|日本企業|日本標的|"
    r"日系大手|国内重要インフラ|"
    r"防衛省|防衛装備|自衛隊|\bJSDF\b)",
    re.IGNORECASE,
)
# 2026-06-17: 事故・運用ミスによる漏洩 (誤送信 / 紛失 / 設定ミス 等) は「敵対的サイバー
# 脅威」ではない。japan_watch (= 日本標的の脅威) から落とすため、事故起因の高精度語を検出。
_ACCIDENTAL_LEAK_PATTERNS = re.compile(
    r"(誤送信|誤添付|誤公開|誤掲載|誤交付|誤配布|誤配信|誤表示|誤廃棄|送信ミス|"
    r"宛先(?:誤|間違|の誤)|宛名間違|操作ミス|設定ミス|誤設定|紛失|置き忘れ|取り違え|"
    r"誤って(?:送信|公開|掲載|添付))",
)
# 敵対的サイバー脅威を示す語 (これがあれば事故扱いしない。「設定ミスを突かれて攻撃」等)。
_ATTACK_INDICATOR_PATTERNS = re.compile(
    r"(攻撃|不正アクセス|ランサム|マルウェア|ウイルス|侵害|悪用|改ざん|フィッシング|"
    r"標的型|窃取|脅迫|乗っ取|なりすま|\bDDoS\b|\bexploit|\bransomware\b|\bmalware\b|"
    r"\bphishing\b|\bbreach(?:ed)?\b|\bhack)",
    re.IGNORECASE,
)


def looks_accidental_leak(text: str) -> bool:
    """text が事故・運用ミス起因の漏洩 (誤送信/紛失/設定ミス 等) を示すか (敵対攻撃語が無い場合)。

    「設定ミスを突かれて攻撃」のように攻撃語があれば事故扱いしない。脅威マップ (#8) と
    japan_watch routing の双方で「インシデント ≠ サイバー攻撃」の precision 是正に使う。
    actor 検出有無 (metadata) は呼出側で別途 AND すること (本関数は text のみで判定)。
    """
    if not text:
        return False
    return bool(_ACCIDENTAL_LEAK_PATTERNS.search(text)) and not bool(
        _ATTACK_INDICATOR_PATTERNS.search(text)
    )


_KEV_PATTERNS = re.compile(
    r"\b(actively\s+exploited|in[- ]the[- ]wild|exploited\s+in\s+attacks|"
    r"KEV\b|CISA[- ]listed|"
    r"weaponized|zero[- ]day|0[- ]day)\b",
    re.IGNORECASE,
)
_ZERO_DAY_PATTERNS = re.compile(
    r"\b(zero[- ]day|0[- ]day|unpatched\s+vulnerability)\b|"
    r"(ゼロデイ|未公開脆弱性|未パッチ)",
    re.IGNORECASE,
)
# 既知 APT アクター名 (簡易、actor_aliases.yaml は別途レジストリで詳細検出)
_KNOWN_APT_PATTERNS = re.compile(
    r"\b(volt\s+typhoon|salt\s+typhoon|silk\s+typhoon|flax\s+typhoon|"
    r"apt\d+|apt[- ]?\d+|"
    r"lazarus|kimsuky|andariel|bluenoroff|"
    r"sandworm|fancy\s+bear|cozy\s+bear|turla|"
    r"mustang\s+panda)\b",
    re.IGNORECASE,
)

_SECURITY_RELEVANT_CATEGORIES = frozenset(
    {"apt", "apt_leak", "vulnerability", "incident", "breach", "advisory", "malware"},
)
_SECURITY_RELEVANT_BROAD = frozenset(
    {
        "apt",
        "apt_leak",
        "vulnerability",
        "incident",
        "breach",
        "advisory",
        "malware",
        "policy",
    },
)


def extract_signals_from_briefing(
    msg: BriefingMessage,
    body_text: str,
    *,
    feed_title: str = "",
    brief_count_24h_snapshot: int = 0,
) -> RoutingSignals:
    """LLM 要約済 BriefingMessage (RSS / web scraper 由来) からシグナル抽出。

    Phase 5L-3: LLM が出した ``routing_flags`` を primary 信号として採用。
    regex は補助 (LLM confidence=low 時 / LLM フラグ欠落時) に格下げ。

    Phase 5T-V: feed_title / brief_count_24h_snapshot を呼び出し側 (main.py) から
    注入して router R6 cap 等で参照する。
    """
    from src.cti.llm_routing_flags import parse_routing_flags  # 遅延インポート (循環回避)

    title = msg.title or ""
    summary = msg.summary or ""
    bluf = msg.bluf or ""
    full_text = f"{title}\n{bluf}\n{summary}\n{body_text or ''}"

    article_type = detect_article_type(
        title,
        llm_hint=str(msg.metadata.get("article_type", "")) or None,
    )

    # Phase 5L-3: LLM 構造化フラグを primary シグナルとして取得
    llm_flags = parse_routing_flags(msg.metadata.get("routing_flags"))

    # regex 由来のシグナル (fallback / 補助)
    regex_japan_general = bool(_JAPAN_GENERAL_PATTERNS.search(full_text))
    regex_japan_critical = bool(_JAPAN_CRITICAL_PATTERNS.search(full_text))

    # 合議: LLM が confidence=high|medium で出した判定を primary、
    # confidence=low や LLM フラグ未提供時のみ regex を採用。
    # Phase 5O: 「Japan-mentioned ≠ Japan-targeted」を厳格化。
    use_llm_primary = llm_flags.confidence in ("high", "medium")
    if use_llm_primary:
        # LLM 主導: japan_targeted=True なら critical 確定。
        # False の場合は regex で誤検知しても overrule (発信者・ベンダー言及の誤検知防止)。
        japan_critical = llm_flags.japan_targeted
        japan_general = llm_flags.japan_targeted or regex_japan_critical
    else:
        # LLM 不確実 → regex フォールバック
        japan_critical = regex_japan_critical
        japan_general = regex_japan_general

    # Phase 5O: japan_security_relevant は「日本標的の **active threat / 被害事案**」
    # を意味するよう厳格化。category=vulnerability (ベンダー製品 advisory) は
    # たとえ Japan-mentioned でも japan_watch から除外し brief 経路に流す。
    # → R3.japan_watch (router) の発火条件を絞る効果。
    #
    # 2026-06-17 修正 (japan_watch 誤分類): 旧実装は `japan_general` / regex に依存し、
    # **日本が標的でなく一言言及されただけ**の事案まで japan_watch に流れていた。
    # 実例: FBI の Outsider Enterprise 摘発 (米)、OpenSSL advisory、韓国クーパン breach
    # (victim=KR だが本文に「日本企業も注意」等で『日本企業』が混入) が誤分類。
    # regex は critical 語 (日本企業 等) でも「韓国だけでなく日本企業も…」のような
    # 非標的文脈で誤発火する。よって **「日本が実際の標的/被害」= (LLM japan_targeted /
    # 抽出 victim_country==JP)** を要求し、regex 単独では japan_watch に流さない。
    # 真の日本被害は victim_country=JP 抽出 or LLM japan_targeted でほぼ捕捉される。
    victim_country_iso = str(msg.metadata.get("victim_country_iso") or "").upper()
    japan_targeted_actual = (use_llm_primary and llm_flags.japan_targeted) or (
        victim_country_iso == "JP"
    )
    # 2026-06-17: 事故・運用ミス起因の漏洩 (誤送信 等) は敵対的サイバー脅威でないので
    # japan_watch から除外。攻撃語 / 検出 actor があれば事故扱いしない (「設定ミスを突かれ
    # 攻撃」等は脅威)。これも「(日本の) インシデント ≠ サイバー攻撃」の precision 是正。
    is_accidental_leak = looks_accidental_leak(full_text) and not bool(
        msg.metadata.get("detected_actor_ids")
    )
    is_japan_security_relevant = (
        japan_targeted_actual
        and msg.category in _SECURITY_RELEVANT_CATEGORIES
        and msg.category != "vulnerability"
        and not is_accidental_leak
    )

    # B2: 派生 signal を local に hoist し、constructor と PIR signals 条件評価の両方へ供給。
    has_kev = (
        bool(_KEV_PATTERNS.search(full_text))
        or (use_llm_primary and llm_flags.is_breaking_critical)
        or _cve_on_kev(msg, full_text)
    )
    is_zero_day = bool(_ZERO_DAY_PATTERNS.search(full_text))
    has_known_apt = (
        bool(_KNOWN_APT_PATTERNS.search(full_text))
        or bool(msg.metadata.get("detected_actor_ids"))
        or (use_llm_primary and bool(llm_flags.primary_actor_id))
    )
    is_security_relevant = msg.category in _SECURITY_RELEVANT_BROAD
    is_apt_leak = msg.category == "apt_leak"

    # 語彙拡張 ① (回収): 既存 metadata から victim_sector / actor nations を取り出す。
    victim_sector = str(msg.metadata.get("victim_sector_canonical") or "")
    _nations_raw = msg.metadata.get("detected_actor_nations")
    threat_actor_nations = (
        frozenset(str(n) for n in _nations_raw if n)
        if isinstance(_nations_raw, list)
        else frozenset()
    )

    # 語彙拡張② user 定義 match_list の判定 (遅延 import で循環回避、cache 済で安価)。
    from src.cti.match_lists import match_lists_for_text

    matched_keyword_lists = match_lists_for_text(full_text)

    return RoutingSignals(
        source="briefing",
        importance=msg.importance,
        article_type=article_type,
        mentions_japan=japan_general,
        mentions_japan_critical=japan_critical,
        is_japan_security_relevant=is_japan_security_relevant,
        has_kev_or_active_exploit=has_kev,
        is_zero_day=is_zero_day,
        has_known_apt=has_known_apt,
        max_cvss=_max_cvss_for(msg, full_text),
        is_security_relevant=is_security_relevant,
        confidence_all_low=False,
        # Phase 5N: APT leak は LLM 判定 category に依存。判定基準は briefing/summarizer.j2
        is_apt_leak=is_apt_leak,
        llm_japan_targeted=llm_flags.japan_targeted,
        llm_is_breaking_critical=llm_flags.is_breaking_critical,
        llm_primary_actor_id=llm_flags.primary_actor_id,
        llm_dedup_key=llm_flags.dedup_key,
        llm_confidence=llm_flags.confidence,
        feed_title=feed_title,
        brief_count_24h_snapshot=brief_count_24h_snapshot,
        category=msg.category or "",
        editorial_stance=llm_flags.editorial_stance,
        victim_sector=victim_sector,
        threat_actor_nations=threat_actor_nations,
        matched_keyword_lists=matched_keyword_lists,
    )


# 旧 markdown Grok 経路の extract_signals_from_grok / _grok_section_to_type は
# Grok JSONL 化 (briefing metadata.target_channel で routing 直接指定) に伴い
# 2026-06-13 撤去。Grok briefing は router を経由しない。
