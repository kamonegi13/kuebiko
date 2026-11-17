"""LEDGER_OCCURRENCE_FLOOR: 発生の確度の下限 (帰属の確度は変えない)。"""

from __future__ import annotations

from dataclasses import replace

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


class _FakeRepo:
    def __init__(self, titles: dict[str, str]) -> None:
        self._titles = titles

    def get_article_grounding(self, article_id: str) -> dict[str, object] | None:
        return {"title": self._titles[article_id]} if article_id in self._titles else None

    def entity_keys_for_articles(self, ids: list[str]) -> dict[str, set[str]]:
        return {}


class _FakeStore:
    def __init__(self, rows: list[tuple[str, str, str, str]]) -> None:
        self._rows = rows

    def evidence_bases(self, situation_id: str) -> list[tuple[str, str, str, str]]:
        return self._rows


def _jc(evidence: tuple[EvidenceItem, ...], claim: str) -> KeyJudgment:
    return replace(_j(evidence), claim=claim)


def test_unrelated_disclosure_title_is_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    j = _jc((_ev("a", "victim_disclosed"),), "Orbitech 社が不正アクセスを受けた")
    repo = _FakeRepo({"a": "Harborline 社が漏えいを公表"})
    assert apply_occurrence_floor(j, repo=repo) is j


def test_related_disclosure_title_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    j = _jc((_ev("a", "victim_disclosed"),), "Orbitech 社が不正アクセスを受けた")
    repo = _FakeRepo({"a": "Orbitech 社が不正アクセスを公表"})
    assert apply_occurrence_floor(j, repo=repo).occurrence_confirmed


def test_high_needs_two_related_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    j = _jc((_ev("a", "victim_disclosed"), _ev("b", "govt_confirmed")), "Orbitech 社が侵害を受けた")
    repo = _FakeRepo({"a": "Orbitech 社が公表", "b": "Harborline の別件"})
    assert apply_occurrence_floor(j, repo=repo).occurrence_confidence == "moderate"


def test_existing_evidence_keeps_mark_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")
    j = _jc((), "Orbitech 社が不正アクセスを受けた")  # 今回の版の新着に公表が無い
    store = _FakeStore([("a", "victim_disclosed", "supports", "t1")])
    repo = _FakeRepo({"a": "Orbitech 社が公表"})
    assert apply_occurrence_floor(j, repo=repo, store=store).occurrence_confirmed


def test_failure_does_not_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "1")

    class _Boom(_FakeRepo):
        def entity_keys_for_articles(self, ids: list[str]) -> dict[str, set[str]]:
            raise RuntimeError("db")

    j = _j((_ev("a", "victim_disclosed"),))
    assert apply_occurrence_floor(j, repo=_Boom({})) is j
