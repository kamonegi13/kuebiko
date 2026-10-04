"""重要度の再設計 — 事象の深刻さと関連性を分けて導く (2026-10-03、記録のみの段 M1・M2)。

設計: docs/importance_relevance_redesign.md。

- **深刻さ** (S3 重大 / S2 注意 / S1 参考): 「何が起きたか」だけで決める。日本・SIR・特定国では
  上げ下げしない。材料は記事の種類ごとに変える (脆弱性 = KEV・悪用・CVSS / 事案 = 深刻度の軸 /
  研究 = 行為者と広がり)。政策・地政学はサイバーの深刻さの対象外 (None) で、代わりに
  **戦略上の重み** を付ける。解説・まとめ・入門は派生記事として深刻さを付けない (元の事象で扱う)
- **関連性**: 日本 (標的 / 被害 / 言及)・注視国・SIR の該当を entity から導く。文の語では決めない。
  SIR の該当は生のまま全部残し、どれを「関連性の核」に数えるかは ``RELEVANCE_CORE_SIRS`` で導く
  (SIR を変えても学習し直さない)

いまの重要度 (high / medium / low) は**置き換えない**。並べて記録し、差を読んでから下流を移す。
決まりごとを変えたら ``RULE_VERSION`` を上げる — 毎時の段が古い版の記録を付け直す。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

#: 版の履歴: .1 初版 / .2 正解集 (Opus 裁定 204 件) で直した — 攻撃者の主張だけは S1・
#: 本文に書かれた CVSS も見る・マルウェアの解析は S2 / .3 複数組織への不正アクセスだけは S2
#: / .4 流出「可能性」の侵害は S2・掲載だけ (暗号化と読まれても) は S1
#: / .5 公的な枠組みとの照合: PoC の公開は CVSS によらず S2 (SSVC)・被害額 1 億ドル以上は S3
#: / .6 記事が触れるだけの古い CVE の KEV 掲載は S3 の根拠にしない (``subject_kev``)
#: / .7 規模を本文の被害の件数で直す (``corrected_axes``)・被害額は被害の文の金額だけ (売上・
#: 販売価格・主張を除く)
#: / .8 軸の無い記事 (事案・マルウェア・研究) は深刻さを付けない (``no_axes``)。日本との関係のため
#: 全記事を記録するようにした (2026-10-04)
RULE_VERSION = "2026-10-04.8"

Severity = Literal["S3", "S2", "S1"]
StrategicWeight = Literal["heavy", "moderate", "light"]
JapanRelation = Literal["targeted", "affected", "mentioned", "none"]

#: 注視国 (関与国の ISO)。関連性と戦略上の重みの両方で使う
WATCHED_NATIONS: frozenset[str] = frozenset({"CN", "RU", "KP", "IR"})

#: 「誰が・どこで」を表す SIR (設計書 §8.1 の推奨、判断待ち)。「何が起きたか」を表す SIR
#: (新しい PoC の脆弱性・重要インフラ・サプライチェーン・機関の勧告) は深刻さの側の話なので数えない
RELEVANCE_CORE_SIRS: frozenset[str] = frozenset(
    {
        "pir_jp_targeted",
        "pir_china_apt",
        "pir_russia_apt",
        "pir_dprk_apt",
        "pir_state_ransomware",
        "pir_integrated_cyber_ops",
        "pir_apt_leak",
        "pir_emergency_alerts",
    }
)

_JP_TARGETED_SIRS = frozenset({"pir_jp_targeted"})
_JP_AFFECTED_SIRS = frozenset({"pir_jp_company_breach"})

_NON_CYBER = frozenset({"geopolitical", "policy", "other"})
_VULN = frozenset({"vulnerability", "advisory"})
#: 派生記事 (事象そのものを報じない)。深刻さは元の事象で扱う
_DERIVATIVE_TYPES = frozenset({"opinion", "recap", "tutorial"})

_WIDE = frozenset({"multi_org_or_provider", "sector_wide", "national"})
_HARMED = frozenset({"unauthorized_access", "data_exposure", "disruption", "destructive"})
_CRITICAL_TARGETS = frozenset({"government", "critical_infra", "defense"})
_PRODUCT_TARGETS = frozenset({"enterprise_product", "ics_ot_product"})
_LARGE_MAGNITUDE = frozenset({"100k_1m", "ge_1m"})

CVSS_CRITICAL = 9.0
CVSS_HIGH = 7.0
MAX_CVSS = 10.0
#: S3 にする被害額 (米ドル)。NIS2 の「重大なインシデント」の金額基準 (50 万ユーロ) は組織単位の
#: 報告義務の線で、ニュースの「重大」はそれより桁が大きい事案に絞る
LARGE_LOSS_USD = 100_000_000.0
#: 規模の区切り (深刻度の軸の magnitude の値と同じ)
MAGNITUDE_1K = 1_000
MAGNITUDE_100K = 100_000
MAGNITUDE_1M = 1_000_000
#: 円 → 米ドルの換算 (被害額の桁を見るだけなので固定の概算でよい)
JPY_PER_USD = 150.0


@dataclass(frozen=True)
class ImportanceInputs:
    """導出の材料 (すべて保存済みの値から組み立てる)。"""

    category: str
    article_type: str
    axes: Mapping[str, str]
    on_kev: bool
    max_cvss: float
    victim_country: str
    involved_countries: frozenset[str]
    mentioned_countries: frozenset[str]
    sir_ids: frozenset[str]
    #: 本文に書かれた被害額 (米ドル換算の最大値、``stated_loss_usd``)。書かれていなければ 0
    loss_usd: float = 0.0


@dataclass(frozen=True)
class ImportanceV2:
    """導出結果 (1 記事 1 行で記録する)。"""

    severity: Severity | None
    severity_basis: str
    strategic_weight: StrategicWeight | None
    jp: JapanRelation
    nations: tuple[str, ...]
    sir_ids: tuple[str, ...]
    relevant: bool
    rule_version: str = RULE_VERSION

    @property
    def level(self) -> int | None:
        """重要度の 6 段階 (1 が最上位)。``importance_level`` を参照。"""
        return importance_level(self.severity, self.relevant)


#: 6 段階の並び = 深刻さを先に見る (案 A、2026-10-04 利用者決定)。関連性ありは記事の約 39% と広く、
#: S3 は絞った判定なので、関連性を先に置くと日本に関わるだけの注意級が悪用中の脆弱性より上に来る
_SEVERITY_RANK: dict[str, int] = {"S3": 0, "S2": 1, "S1": 2}


def importance_level(severity: Severity | None, relevant: bool) -> int | None:
    """深刻さ × 関連性 → 1 (S3・関連あり) 〜 6 (S1・関連なし)。深刻さが無ければ None。

    政策・地政学・派生記事・軸なしは 6 段階に入れない (戦略上の重み・元の事象で扱う)。
    """
    if severity is None:
        return None
    return _SEVERITY_RANK[severity] * 2 + (1 if relevant else 2)


def derive(inp: ImportanceInputs) -> ImportanceV2:
    """深刻さ・戦略上の重み・関連性を導く (副作用なし)。"""
    severity, basis = derive_severity(inp)
    jp = japan_relation(inp)
    nations = tuple(sorted(inp.involved_countries & WATCHED_NATIONS))
    return ImportanceV2(
        severity=severity,
        severity_basis=basis,
        strategic_weight=strategic_weight(inp, nations),
        jp=jp,
        nations=nations,
        sir_ids=tuple(sorted(inp.sir_ids)),
        relevant=is_relevant(jp, nations, inp.sir_ids),
    )


def derive_severity(inp: ImportanceInputs) -> tuple[Severity | None, str]:
    """深刻さと、その根拠の短い名前 (どの決まりごとに当たったか)。"""
    if inp.category in _NON_CYBER:
        return None, "non_cyber"
    if inp.article_type in _DERIVATIVE_TYPES:
        return None, "derivative"
    if inp.on_kev:
        return "S3", "kev"
    if inp.category in _VULN:
        return _vuln_severity(inp)
    ax = inp.axes
    # 軸がまだ付いていない記事 (日本との関係のために全記事を記録する — 2026-10-04) は判断しない。
    # 空の軸で導くと事案は S1・マルウェアは S2 になり、「未判断」が「参考」に化ける
    if not ax:
        return None, "no_axes"
    # 暴露サイトへの掲載 (攻撃者の主張だけ) は、カテゴリがマルウェアでも事案として扱う
    if inp.category == "malware" and ax.get("confirmation") != "claimed_only":
        if ax.get("scope") in _WIDE and ax.get("actor") == "state":
            return "S3", "malware_state_wide"
        # 新しいマルウェア・キャンペーンの解析は「新しい手口を含む脅威の分析」(正解集で S2)
        return "S2", "malware"
    if inp.category == "research":
        if ax.get("actor") == "state" or ax.get("scope") in _WIDE:
            return "S2", "research_state_or_wide"
        return "S1", "research"
    return _incident_severity(ax, inp.loss_usd)


def _vuln_severity(inp: ImportanceInputs) -> tuple[Severity, str]:
    exploitation = inp.axes.get("exploitation")
    if exploitation == "exploited_in_wild":
        return "S3", "exploited"
    if inp.max_cvss >= CVSS_CRITICAL:
        return "S2", "cvss_critical"
    # SSVC (CISA/SEI) の悪用状況 none / public PoC / active に合わせ、PoC の公開は CVSS に
    # よらず S2 (新しい脆弱性は公表時点で点数が付いていないことが多い)
    if exploitation == "poc":
        return "S2", "poc"
    return "S1", "vuln"


def _incident_severity(ax: Mapping[str, str], loss_usd: float = 0.0) -> tuple[Severity, str]:
    scope, impact, conf = ax.get("scope"), ax.get("impact"), ax.get("confirmation")
    actor, target = ax.get("actor"), ax.get("target")
    confirmed = conf == "confirmed"
    # 複数組織への不正アクセスだけ (フィッシング・キャンペーンの解析など) は S2 — 正解集では
    # 情報の漏えい・停止・破壊を伴うか、業界・国規模に及ぶものだけが S3
    if (
        confirmed
        and impact in _HARMED
        and (
            scope in {"sector_wide", "national"}
            or (scope in _WIDE and impact != "unauthorized_access")
        )
    ):
        return "S3", "confirmed_wide_harm"
    if actor == "state" and target in _CRITICAL_TARGETS:
        return "S3", "state_on_critical"
    if confirmed and impact in {"disruption", "destructive"} and target in _CRITICAL_TARGETS:
        return "S3", "critical_disruption"
    if ax.get("exploitation") == "exploited_in_wild" and target in _PRODUCT_TARGETS:
        return "S3", "exploited_product"
    if impact in _HARMED and ax.get("magnitude") in _LARGE_MAGNITUDE:
        return "S3", "large_magnitude"
    # 件数でなく金額で表される被害 (暗号資産の窃取・詐欺など)。重要インフラの定義は広げず規模で拾う
    if impact in _HARMED and loss_usd >= LARGE_LOSS_USD:
        return "S3", "large_loss"
    # 侵害そのものは確認され、流出が「可能性」の段階のものも単一組織の侵害として S2
    if impact in _HARMED and conf in {"confirmed", "possible"}:
        return "S2", "harm"
    # 攻撃者の主張だけ (暴露サイトへの掲載など) は、業務停止の報道がなければ参考扱い。
    # ランサムウェアの掲載は暗号化 (destructive) と読まれやすいので、停止の報道だけを S2 にする
    if impact == "disruption" and conf == "claimed_only":
        return "S2", "claimed_disruption"
    if ax.get("exploitation") in {"poc", "exploited_in_wild"} or actor == "state":
        return "S2", "poc_or_state"
    return "S1", "incident"


def strategic_weight(inp: ImportanceInputs, nations: Iterable[str]) -> StrategicWeight | None:
    """政策・地政学の記事の戦略上の重み (サイバーの記事には付けない)。

    注視国が関与する出来事の報道 = 重 / 注視国を扱う論評・まとめ = 中 / 注視国が主体でない = 軽。
    """
    if inp.category not in {"geopolitical", "policy"}:
        return None
    if not tuple(nations):
        return "light"
    return "moderate" if inp.article_type in _DERIVATIVE_TYPES else "heavy"


def japan_relation(inp: ImportanceInputs) -> JapanRelation:
    """日本との関係の強さ (標的 > 被害 > 言及)。"""
    if inp.sir_ids & _JP_TARGETED_SIRS:
        return "targeted"
    if inp.victim_country == "JP" or inp.sir_ids & _JP_AFFECTED_SIRS:
        return "affected"
    if "JP" in inp.involved_countries or "JP" in inp.mentioned_countries:
        return "mentioned"
    return "none"


#: 旧 1 本化 facet ``level_filter`` (top/notable/relevant) → 新 3 独立 facet
#: (``min_severity``, ``relevant_only``, ``include_strategic``) への写像 (2026-10-04)。
#: 「関連性ありの重大」のように旧 facet では表せない組み合わせが選べないという利用者指摘を
#: 受けて 3 独立 facet に置き換えたが、旧パラメータ (API 後方互換・保存済み widget 設定の
#: 移行前) を引き続き解釈できるよう、この写像を SSoT として両方から参照する。
#: "top"=重大のみ / "notable"=注意以上 (旧仕様で軸なし heavy も含んでいた → toggle on) /
#: "relevant"=関連性あり (旧仕様は深刻さ無し記事を除いていた → 参考以上 + 関連性のみ)。
def legacy_level_filter_to_severity(level_filter: str | None) -> tuple[str, bool, bool]:
    """``(min_severity, relevant_only, include_strategic)`` を返す。不明な値は絞り込み無し。"""
    if level_filter == "top":
        return "S3", False, False
    if level_filter == "notable":
        return "S2", False, True
    if level_filter == "relevant":
        return "S1", True, False
    return "", False, False


def is_relevant(jp: JapanRelation, nations: Iterable[str], sir_ids: Iterable[str]) -> bool:
    """関連性あり = 日本が標的・被害 / 注視国が関与 / 関連性の核の SIR に該当。"""
    if jp in {"targeted", "affected"}:
        return True
    if tuple(nations):
        return True
    return bool(RELEVANCE_CORE_SIRS & set(sir_ids))


#: 本文に書かれた CVSS の値 (「CVSS 9.8」「CVSSv3.1: 9.8」「CVSS スコア 9.8」等)。
#: CVSS の直後の版 (v3.1 / 4.0) は値ではないので先に読み飛ばす
_CVSS_HEAD = re.compile(r"CVSS(?:\s*(?:v|version)?\s*[234]\.[01x])?", re.IGNORECASE)
_SCORE = re.compile(r"(?<![\d.])(\d{1,2}\.\d)(?![\d.])")
_CVSS_WINDOW = 30
#: 値は無いが「critical / クリティカル / 緊急」と CVSS の区分だけ書かれている場合 (= 9.0 以上)
_CVSS_CRITICAL = re.compile(
    r"CVSS[^\n]{0,40}?(critical|クリティカル|緊急)|(critical|クリティカル|緊急)[^\n]{0,20}?CVSS",
    re.IGNORECASE,
)


def stated_cvss(text: str) -> float:
    """本文に書かれた CVSS の最大値 (書かれていなければ 0)。

    手元の NVD の記録は新しい CVE をまだ採点していないことが多い
    (正解集の CVE 記事 47 件中 11 件で欠落)。
    """
    best = 0.0
    for head in _CVSS_HEAD.finditer(text):
        window = text[head.end() : head.end() + _CVSS_WINDOW].split("\n", 1)[0]
        m = _SCORE.search(window)
        if m and float(m.group(1)) <= MAX_CVSS:
            best = max(best, float(m.group(1)))
    if best == 0.0 and _CVSS_CRITICAL.search(text):
        return CVSS_CRITICAL
    return best


_USD_AMOUNT = re.compile(
    r"(?:US)?\$\s?(\d[\d,]*(?:\.\d+)?)\s*(billion|million|bn|b|m)?(?![a-z])", re.IGNORECASE
)
_JA_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(兆|億|万)?\s*(米ドル|ドル|円)")
_EN_SCALE = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "m": 1e6}
_JA_SCALE = {"兆": 1e12, "億": 1e8, "万": 1e4}


def _num(raw: str) -> float:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return 0.0


_SENTENCE = re.compile(r"[。\n]|(?<=[a-z0-9])\.\s")
#: 攻撃者の主張・販売・企業の規模・送信数の文 (§4: 主張された件数・規模では重くしない。売上は
#: 被害額でなく、送信したメールの数は被害者の数でない)
_CLAIM_OR_SIZE_WORDS = re.compile(
    r"主張|販売|売上|売り上げ|収益|身代金|要求|ダークウェブ|フォーラム|送信|"
    r"claim|for sale|sold|selling|revenue|forum|advertis|ransom|demand",
    re.IGNORECASE,
)
#: 金額を被害額と読む文の語
_LOSS_WORDS = re.compile(
    r"盗|窃取|被害|損害|流出|詐取|不正送金|stole|steal|drain|theft|loss|lost|heist|launder",
    re.IGNORECASE,
)


#: 件数・金額を打ち消す文 (要約が「70 万件は送信数で、流出ではありません」と注記する等)
_NEGATION_WORDS = re.compile(
    r"ではありません|ではない|できません|とは言えない|否定|確認されていな|確認されていません|"
    r"not the|no evidence|did not|does not",
    re.IGNORECASE,
)
#: 言い方の引用だけの文 (「『45万人分が漏えいした』とは言えない」の引用部分)
_QUOTE_ONLY = re.compile(r"^\s*[-・]?\s*「[^」]*」\s*$")


def _sentences(text: str, must: re.Pattern[str]) -> list[str]:
    """``must`` の語を含み、主張・販売・企業規模・打ち消しの語を含まない文。"""
    return [
        s
        for s in _SENTENCE.split(text)
        if must.search(s)
        and not _CLAIM_OR_SIZE_WORDS.search(s)
        and not _NEGATION_WORDS.search(s)
        and not _QUOTE_ONLY.match(s)
    ]


def stated_loss_usd(text: str) -> float:
    """本文に書かれた被害額の最大値 (米ドル換算)。

    被害の語のある文の金額だけを読む。売上・販売価格・身代金の要求額・攻撃者の主張は数えない
    (正解集で被害額を根拠にした S3 の誤り 4 件中 3 件が売上・アクセス権の販売価格だった)。
    """
    best = 0.0
    for sent in _sentences(text, _LOSS_WORDS):
        for m in _USD_AMOUNT.finditer(sent):
            best = max(best, _num(m.group(1)) * _EN_SCALE.get((m.group(2) or "").lower(), 1.0))
        for m in _JA_AMOUNT.finditer(sent):
            v = _num(m.group(1)) * _JA_SCALE.get(m.group(2) or "", 1.0)
            best = max(best, v / JPY_PER_USD if m.group(3) == "円" else v)
    return best


#: 被害の件数 (人・記録・アカウント)。「31万2000件」「1万2345人」のように万の後ろの端数も読む
_JA_COUNT = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(億|万)?(\d[\d,]*)?\s*(件|人分|人|名|アカウント|レコード)"
)
_EN_COUNT = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(million|billion|bn|k)?\s+(?:[a-z-]+\s+){0,2}?"
    r"(records|users|customers|accounts|people|individuals|patients|members|subscribers|employees)",
    re.IGNORECASE,
)
_EN_COUNT_SCALE = {"million": 1e6, "billion": 1e9, "bn": 1e9, "k": 1e3}
#: 件数を被害の数と読む文の語 (同じ文に無ければ数えない — 利用者数・市場規模を拾わない)
_HARM_WORDS = re.compile(
    r"流出|漏えい|漏洩|窃取|盗まれ|盗難|閲覧され|不正アクセス|改ざん|侵害|暴露|公開され|複製|"
    r"leak|breach|expos|stolen|steal|compromis|exfiltrat|accessed|copied",
    re.IGNORECASE,
)
#: 件数の直後がこれなら被害者の数ではない (送信したメール・試行・ファイル・脆弱性)
_NOT_VICTIM_AFTER = re.compile(
    r"^\s*(?:超|以上|程度)?\s*の?\s*(?:不審な?|迷惑|スパム)?\s*"
    r"(メール|フィッシング|攻撃|試行|脆弱性|ファイル|送金|ダウンロード|通知)"
)


def stated_victim_count(text: str) -> int:
    """本文に書かれた被害の件数の最大値 (書かれていなければ 0)。

    深刻度の軸の「規模」は要約から付けるため、本文の件数が抜ける (正解集の無作為抽出で S3 の
    見逃し 2 件) 一方、件数の無い記事に大きな規模を付ける (規模を根拠の S3 は 20 件中 14 件が誤り)。
    被害の語のある文の、人・記録・アカウントの数だけを数える。ダウンロード数・送金数・送信数・
    ファイル数・攻撃者が主張する件数は被害者の数ではない (基準の文 §4)。
    """
    best = 0
    for sent in _sentences(text, _HARM_WORDS):
        for m in _JA_COUNT.finditer(sent):
            if _NOT_VICTIM_AFTER.match(sent[m.end() :]):
                continue
            head = _num(m.group(1)) * _JA_SCALE.get(m.group(2) or "", 1.0)
            best = max(best, int(head + (_num(m.group(3)) if m.group(3) else 0)))
        for m in _EN_COUNT.finditer(sent):
            scale = _EN_COUNT_SCALE.get((m.group(2) or "").lower(), 1.0)
            best = max(best, int(_num(m.group(1)) * scale))
    return best


def corrected_axes(axes: Mapping[str, str], victim_count: int) -> dict[str, str]:
    """深刻度の軸の「規模」を本文の件数で直す (版 .7)。

    - 本文に被害の件数があれば、それで規模を決める (上げも下げもする)
    - 件数が本文に無いのに 10 万件以上の規模が付いていれば「不明」に戻す (要約からの過大な推定)
    - 攻撃者の主張だけの事案は、件数があっても大きな規模にしない (基準の文 §4)

    正解集 (Opus 5.5) の測定用 250 件で、S3 の適合率 71% → 77%・深刻さの正解率 85% → 89%
    (s21 の軸)。無作為 200 件の見逃し 7 件中 2 件を拾い、新たな誤った S3 は 0 件。
    """
    out = dict(axes)
    if not out:
        return out
    if victim_count:
        out["magnitude"] = magnitude_of(victim_count)
    if out.get("magnitude") in _LARGE_MAGNITUDE and (
        not victim_count or out.get("confirmation") == "claimed_only"
    ):
        out["magnitude"] = "unknown"
    return out


def magnitude_of(count: int) -> str:
    """件数 → 深刻度の軸の「規模」の値。"""
    if count >= MAGNITUDE_1M:
        return "ge_1m"
    if count >= MAGNITUDE_100K:
        return "100k_1m"
    return "1k_100k" if count >= MAGNITUDE_1K else "lt_1k"


#: 記事の主題の CVE とみなす年の幅 (今年と前年)。過去の修正済み CVE への言及を KEV の根拠にしない
SUBJECT_CVE_YEARS = 2
_CVE_YEAR = re.compile(r"^CVE-(\d{4})-", re.IGNORECASE)


def subject_kev(cves: Iterable[str], kev: frozenset[str], year: int) -> bool:
    """記事の主題とみなせる新しい CVE が KEV に載っているか。

    正解集 (版 .6) の定め: 記事が触れるだけの古い CVE (既に修正済み) が KEV に載っていても
    S3 にしない。
    記事の主題の CVE を決定論では特定できないので、今年と前年の CVE に限ることで近似する。
    """
    for c in cves:
        m = _CVE_YEAR.match(c)
        if m and int(m.group(1)) > year - SUBJECT_CVE_YEARS and c.upper() in kev:
            return True
    return False
