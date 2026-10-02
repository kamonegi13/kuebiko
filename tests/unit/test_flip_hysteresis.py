"""低確度の見立ての反転は 1 回では確定させない (2026-10-02)。

通し監査: 直近 30 日の反転のうち、低確度の反転は 99 件中 31 件 (31%) が次の改訂で元に戻った
(中 20%・高 13%)。PIR ブリーフの「答えが動いた」にこの揺れも数えられていた。
低確度の反転は候補として記録し、次の再評価でも同じ見立てが首位なら確定する。
"""

from __future__ import annotations

import pytest

from src.assessment.situation_store import RevisionRow
from src.assessment.stateful import _delta_note, _hold_unconfirmed_flip
from src.synthesis.grounded.estimate import HypothesisScore, KeyJudgment


def _prev(
    leading: str = "criminal_financial", note: str = "", claim: str = "前回の答え"
) -> RevisionRow:
    return RevisionRow(
        situation_id="s-1",
        rev=3,
        claim=claim,
        claim_type="ongoing_activity",
        leading_hypothesis=leading,
        confidence="moderate",
        confidence_basis="",
        hypotheses_json="[]",
        assumptions_json="[]",
        missing_json="[]",
        indicators_json="[]",
        implication="",
        delta_type="no_change",
        delta_note=note,
        created_at="2026-10-01T00:00:00+00:00",
    )


def _j(leading: str, confidence: str, claim: str = "新しい答え") -> KeyJudgment:
    return KeyJudgment(
        id="s-1",
        claim=claim,
        domain="cyber_incident",
        leading_hypothesis=leading,
        confidence=confidence,  # type: ignore[arg-type]
        confidence_basis="",
        hypotheses=(
            HypothesisScore(hypothesis=leading, consistent=3, inconsistent=0, verdict="leading"),
        ),
        evidence=(),
    )


def test_first_low_confidence_flip_is_held_with_candidate_note() -> None:
    held, note = _hold_unconfirmed_flip(
        _j("unverified_or_false", "low"), _prev(), was_dormant=False
    )

    assert held.leading_hypothesis == "criminal_financial"
    assert held.confidence == "moderate"
    assert held.claim == "前回の答え"
    assert "見立ての候補: unverified_or_false" in note


def test_second_consecutive_low_flip_to_same_candidate_is_committed() -> None:
    prev = _prev(note="見立ての候補: unverified_or_false (低確度・次の評価で確認)")

    got, note = _hold_unconfirmed_flip(_j("unverified_or_false", "low"), prev, was_dormant=False)

    assert got.leading_hypothesis == "unverified_or_false"
    assert "2 回続けて" in note


def test_candidate_note_survives_other_markers() -> None:
    prev = _prev(note="見立ての候補: state_espionage (低確度・次の評価で確認) / ⚑ 指標発火: x")

    got, _ = _hold_unconfirmed_flip(_j("state_espionage", "low"), prev, was_dormant=False)

    assert got.leading_hypothesis == "state_espionage"


@pytest.mark.parametrize("confidence", ["moderate", "high"])
def test_flip_with_moderate_or_high_confidence_is_committed_at_once(confidence: str) -> None:
    got, note = _hold_unconfirmed_flip(
        _j("state_espionage", confidence), _prev(), was_dormant=False
    )

    assert got.leading_hypothesis == "state_espionage"
    assert note == ""


def test_no_flip_or_dormant_is_untouched() -> None:
    same = _j("criminal_financial", "low")
    assert _hold_unconfirmed_flip(same, _prev(), was_dormant=False) == (same, "")
    flip = _j("state_espionage", "low")
    assert _hold_unconfirmed_flip(flip, _prev(), was_dormant=True) == (flip, "")


def test_switch_off_restores_immediate_flip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_FLIP_HYSTERESIS", "0")
    flip = _j("state_espionage", "low")

    assert _hold_unconfirmed_flip(flip, _prev(), was_dormant=False) == (flip, "")


def test_flip_note_says_when_claim_was_not_revised() -> None:
    """見立てが変わったのに結論文が前回のまま (反転の 6 割) を読み手に見せる。"""
    note = _delta_note(
        _prev(), _j("state_espionage", "high", claim="前回の答え"), "hypothesis_flip"
    )

    assert "結論文は未改訂" in note


def test_flip_backed_by_strong_refutation_is_committed_at_once() -> None:
    """確かな出所の反証が根拠の反転は保留しない (アンカリングしない対称原則が優先)。"""
    from src.synthesis.grounded.estimate import EvidenceItem

    j = _j("accidental_negligence", "low")
    refuting = EvidenceItem(
        article_id="a1",
        excerpt="組織的作戦の痕跡は否定",
        attribution_basis="vendor_confirmed",
        source_tier="research",
        polarity="contradicts",
    )
    from dataclasses import replace

    got, note = _hold_unconfirmed_flip(replace(j, evidence=(refuting,)), _prev(), was_dormant=False)

    assert got.leading_hypothesis == "accidental_negligence"
    assert note == ""
