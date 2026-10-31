"""日本の単一組織の侵害を台帳の情勢として開くかの規則 (P0-30a、純関数)。

開くかは組織の数でなく **関連性と深刻さ** で決める (10-29 判断 J2、設計 v4 §1.2-b):

- 開く (``open``): (i) 優先度の高い SIR (防衛・政府・重要インフラ・国家系) に当たる /
  (ii) 深刻さ S2 以上 / (iii) 続報が出た / (iv) 波及 (複数組織へ広がった) が出た。
- 開かない (``article_only``): 上のどれにも当たらない小規模な単一組織の事案。配信・検索・
  PIR の評価には記事単位で残る。``pir_jp_company_breach`` に当たるだけでは開かない
  (この SIR は triage の importance の下限であって台帳の開設の基準ではない)。
- 保留 (``defer``): 深刻さの軸がまだ無く、他の根拠も無いとき。軸が付いてから再判定する。

⚠ **配線なし**: このモジュールは DB・LLM・設定を読まない。detect / stateful / runner からは
呼ばない (P0-30b が旗 ``DETECT_SINGLE_ORG_RULE`` の背後で配線する)。入力の組み立て
(日本判定・SIR 照合・深刻さ・続報の判定) は呼び手の責務で、ここでは決めない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Verdict = Literal["open", "article_only", "defer"]

#: 優先度の高い SIR (``target_importance=high`` のうち防衛・政府・重要インフラ・国家系の 6 本、
#: 設計 v4 §9)。14 本に広げるかは RP-3 の調整側で決める (U-V4) ので呼び手が差し替えられる。
PRIORITY_SIRS: frozenset[str] = frozenset(
    {
        "pir_critical_infra",
        "pir_jp_targeted",
        "pir_china_apt",
        "pir_dprk_apt",
        "pir_russia_apt",
        "pir_state_ransomware",
    }
)

_JAPAN_ISO = "JP"
#: 単一組織とみなす被害組織の数
_SINGLE_ORG_COUNT = 1
#: ``article_severity_axes.scope`` のうち、単一組織の外へ広がった値 (規則の対象外)
_WIDE_SCOPES = frozenset({"multi_org_or_provider", "sector_wide", "national"})
#: 開設の根拠になる深刻さ (S2 以上)
_OPENING_SEVERITIES = frozenset({"S2", "S3"})

BASIS_SEVERITY = "severity:S2+"
BASIS_FOLLOW_UP = "follow_up"
BASIS_SPILLOVER = "spillover"
BASIS_AXES_PENDING = "axes_pending"
_SIR_PREFIX = "sir:"


@dataclass(frozen=True)
class SingleOrgCandidate:
    """detect の候補 1 件の、規則が見る事実。"""

    victim_country_iso: str | None
    victim_org_count: int
    scope: str | None
    matched_sirs: frozenset[str]
    severity: str | None
    has_follow_up: bool = False
    has_spillover: bool = False


@dataclass(frozen=True)
class SingleOrgDecision:
    """規則の結論と根拠 (``sir:<id>`` / ``severity:S2+`` / ``follow_up`` / ``spillover``)。"""

    verdict: Verdict
    basis: tuple[str, ...]


def is_japan_single_org(candidate: SingleOrgCandidate) -> bool:
    """日本の単一組織の侵害か (被害国 JP ∧ 被害組織 1 ∧ 範囲が単一組織の外でない)。"""
    iso = (candidate.victim_country_iso or "").upper()
    return (
        iso == _JAPAN_ISO
        and candidate.victim_org_count == _SINGLE_ORG_COUNT
        and candidate.scope not in _WIDE_SCOPES
    )


def _opening_basis(candidate: SingleOrgCandidate, priority_sirs: frozenset[str]) -> tuple[str, ...]:
    sirs = sorted(candidate.matched_sirs & priority_sirs)
    basis = [f"{_SIR_PREFIX}{sir}" for sir in sirs]
    if candidate.severity in _OPENING_SEVERITIES:
        basis.append(BASIS_SEVERITY)
    if candidate.has_follow_up:
        basis.append(BASIS_FOLLOW_UP)
    if candidate.has_spillover:
        basis.append(BASIS_SPILLOVER)
    return tuple(basis)


def decide_single_org_open(
    candidate: SingleOrgCandidate,
    *,
    priority_sirs: frozenset[str] = PRIORITY_SIRS,
) -> SingleOrgDecision | None:
    """候補に open / article_only / defer を返す。日本の単一組織でない候補は None (対象外)。"""
    if not is_japan_single_org(candidate):
        return None
    basis = _opening_basis(candidate, priority_sirs)
    if basis:
        return SingleOrgDecision("open", basis)
    if candidate.severity is None:
        return SingleOrgDecision("defer", (BASIS_AXES_PENDING,))
    return SingleOrgDecision("article_only", ())
