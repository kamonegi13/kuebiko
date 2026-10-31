"""単一組織の開設の規則 (P0-30a) の純関数のテスト。配線なし。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from src.assessment.single_org_open import (
    PRIORITY_SIRS,
    SingleOrgCandidate,
    SingleOrgDecision,
    decide_single_org_open,
)

_BASE = SingleOrgCandidate(
    victim_country_iso="JP",
    victim_org_count=1,
    scope="single_org",
    matched_sirs=frozenset(),
    severity="S1",
)


def _decide(**changes: Any) -> SingleOrgDecision | None:
    return decide_single_org_open(replace(_BASE, **changes))


def test_defense_sir_opens_with_sir_basis() -> None:
    d = _decide(matched_sirs=frozenset({"pir_critical_infra"}))
    assert d is not None
    assert d.verdict == "open"
    assert d.basis == ("sir:pir_critical_infra",)


def test_small_non_sir_incident_is_article_only() -> None:
    d = _decide()
    assert d is not None and d.verdict == "article_only"
    assert d.basis == ()


def test_company_breach_sir_alone_does_not_open() -> None:
    d = _decide(matched_sirs=frozenset({"pir_jp_company_breach"}))
    assert d is not None and d.verdict == "article_only"


@pytest.mark.parametrize("severity", ["S2", "S3"])
def test_severity_s2_or_higher_opens(severity: str) -> None:
    d = _decide(severity=severity)
    assert d is not None and d.verdict == "open"
    assert d.basis == ("severity:S2+",)


def test_follow_up_opens_later() -> None:
    d = _decide(has_follow_up=True)
    assert d is not None and d.verdict == "open" and d.basis == ("follow_up",)


def test_spillover_opens_later() -> None:
    d = _decide(has_spillover=True)
    assert d is not None and d.verdict == "open" and d.basis == ("spillover",)


def test_multiple_bases_are_listed_in_stable_order() -> None:
    d = _decide(
        matched_sirs=frozenset({"pir_jp_targeted", "pir_china_apt"}),
        severity="S3",
        has_follow_up=True,
    )
    assert d is not None
    assert d.basis == (
        "sir:pir_china_apt",
        "sir:pir_jp_targeted",
        "severity:S2+",
        "follow_up",
    )


def test_missing_severity_without_other_basis_defers() -> None:
    d = _decide(severity=None)
    assert d is not None and d.verdict == "defer"
    assert d.basis == ("axes_pending",)


def test_missing_severity_but_sir_hit_still_opens() -> None:
    d = _decide(severity=None, matched_sirs=frozenset({"pir_dprk_apt"}))
    assert d is not None and d.verdict == "open"


@pytest.mark.parametrize(
    "changes",
    [
        {"victim_country_iso": "US"},
        {"victim_country_iso": None},
        {"victim_org_count": 2},
        {"victim_org_count": 0},
        {"scope": "multi_org_or_provider"},
        {"scope": "sector_wide"},
        {"scope": "national"},
    ],
)
def test_not_applicable_returns_none(changes: dict[str, Any]) -> None:
    assert _decide(**changes) is None


def test_lowercase_country_is_accepted() -> None:
    assert _decide(victim_country_iso="jp") is not None


def test_priority_sirs_are_the_six_high_importance_ones() -> None:
    assert (
        frozenset(
            {
                "pir_critical_infra",
                "pir_jp_targeted",
                "pir_china_apt",
                "pir_dprk_apt",
                "pir_russia_apt",
                "pir_state_ransomware",
            }
        )
        == PRIORITY_SIRS
    )


def test_custom_priority_sirs_override() -> None:
    cand = replace(_BASE, matched_sirs=frozenset({"pir_x"}))
    d = decide_single_org_open(cand, priority_sirs=frozenset({"pir_x"}))
    assert d is not None and d.basis == ("sir:pir_x",)


def test_inputs_are_not_mutated() -> None:
    cand = replace(_BASE, matched_sirs=frozenset({"pir_china_apt"}))
    decide_single_org_open(cand)
    assert cand.matched_sirs == frozenset({"pir_china_apt"})
