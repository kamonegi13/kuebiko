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
RULE_VERSION = "2026-10-03.5"

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


def stated_loss_usd(text: str) -> float:
    """本文に書かれた金額の最大値 (米ドル換算)。被害額かどうかは呼び手が被害の軸で絞る。"""
    best = 0.0
    for m in _USD_AMOUNT.finditer(text):
        best = max(best, _num(m.group(1)) * _EN_SCALE.get((m.group(2) or "").lower(), 1.0))
    for m in _JA_AMOUNT.finditer(text):
        v = _num(m.group(1)) * _JA_SCALE.get(m.group(2) or "", 1.0)
        best = max(best, v / JPY_PER_USD if m.group(3) == "円" else v)
    return best
