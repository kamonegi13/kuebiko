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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

RULE_VERSION = "2026-10-03.1"

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
    if inp.category == "malware" and ax.get("scope") in _WIDE:
        return (
            ("S3", "malware_state_wide")
            if ax.get("actor") == "state"
            else (
                "S2",
                "malware_wide",
            )
        )
    if inp.category == "research":
        if ax.get("actor") == "state" or ax.get("scope") in _WIDE:
            return "S2", "research_state_or_wide"
        return "S1", "research"
    return _incident_severity(ax)


def _vuln_severity(inp: ImportanceInputs) -> tuple[Severity, str]:
    exploitation = inp.axes.get("exploitation")
    if exploitation == "exploited_in_wild":
        return "S3", "exploited"
    if inp.max_cvss >= CVSS_CRITICAL:
        return "S2", "cvss_critical"
    if inp.max_cvss >= CVSS_HIGH and exploitation == "poc":
        return "S2", "cvss_high_poc"
    return "S1", "vuln"


def _incident_severity(ax: Mapping[str, str]) -> tuple[Severity, str]:
    scope, impact, conf = ax.get("scope"), ax.get("impact"), ax.get("confirmation")
    actor, target = ax.get("actor"), ax.get("target")
    confirmed = conf == "confirmed"
    if confirmed and scope in _WIDE and impact in _HARMED:
        return "S3", "confirmed_wide_harm"
    if actor == "state" and target in _CRITICAL_TARGETS:
        return "S3", "state_on_critical"
    if confirmed and impact in {"disruption", "destructive"} and target in _CRITICAL_TARGETS:
        return "S3", "critical_disruption"
    if ax.get("exploitation") == "exploited_in_wild" and target in _PRODUCT_TARGETS:
        return "S3", "exploited_product"
    if impact in _HARMED and ax.get("magnitude") in _LARGE_MAGNITUDE:
        return "S3", "large_magnitude"
    if impact in _HARMED and conf in {"confirmed", "claimed_only"}:
        return "S2", "harm"
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
