"""LEDGER_OCCURRENCE_FLOOR: 発生の確度の下限 (帰属の確度は変えない)。"""

from __future__ import annotations

import pytest

from src.assessment.occurrence_floor import FLAG_ENV, apply_occurrence_floor
from src.assessment.salience import epistemic_weight
from src.synthesis.grounded.estimate import (
    EvidenceItem,
    KeyJudgment,
    occurrence_floor,
    occurrence_from_basis,
)


def _ev(aid: str, basis: str, polarity: str = "neutral") -> EvidenceItem:
    return EvidenceItem(aid, "t1", basis, "x", polarity)  # type: ignore[arg-type]


def _j(evidence: tuple[EvidenceItem, ...], claim_type: str = "discrete_event") -> KeyJudgment:
    return KeyJudgment(
        id="s-1",
        claim="c",
        domain="cyber_incident",
        leading_hypothesis="unverified_or_false",
        confidence="low",
        confidence_basis="ACH=low",
        hypotheses=(),
        evidence=evidence,
        claim_type=claim_type,
    )


def test_floor_moderate_for_single_disclosure() -> None:
    got = occurrence_floor("discrete_event", (_ev("a", "victim_disclosed"),))
    assert got is not None and got[0] == "moderate" and "被害組織の公表" in got[1]


def test_floor_high_for_two_sources() -> None:
    got = occurrence_floor(
        "discrete_event", (_ev("a", "victim_disclosed"), _ev("b", "govt_confirmed", "contradicts"))
    )
    assert got is not None and got[0] == "high"


def test_floor_none_for_other_types_or_weak_basis() -> None:
    assert occurrence_floor("ongoing_activity", (_ev("a", "victim_disclosed"),)) is None
    assert occurrence_floor("discrete_event", (_ev("a", "claimed_by_actor"),)) is None


def test_flag_zero_is_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLAG_ENV, raising=False)
    j = _j((_ev("a", "victim_disclosed"),))
    assert apply_occurrence_floor(j) is j


def test_shadow_does_not_change(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "shadow")
    j = _j((_ev("a", "victim_disclosed"),))
    assert apply_occurrence_floor(j) is j
    assert not j.occurrence_confirmed


def test_on_marks_basis_keeps_confidence_and_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    j = _j((_ev("a", "victim_disclosed"),))
    out = apply_occurrence_floor(j)
    assert out.confidence == "low"
    assert "発生: 確認済 (被害組織の公表)" in out.confidence_basis
    assert out.occurrence_confirmed and out.occurrence_confidence == "moderate"
    assert occurrence_from_basis(out.confidence_basis) == "moderate"
    # 旗 1 のときだけ並びの重みが発生の確度を使う (leading=unverified の減衰は別)
    assert epistemic_weight(out) > epistemic_weight(j)
    monkeypatch.setenv(FLAG_ENV, "shadow")
    assert epistemic_weight(out) == epistemic_weight(j)


def test_non_event_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    j = _j((_ev("a", "victim_disclosed"),), claim_type="structural")
    assert apply_occurrence_floor(j) is j
