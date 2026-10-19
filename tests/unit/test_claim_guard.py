"""反転確定時の claim 書き直しと、問いの主語の関門 (claim_guard)。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.assessment import claim_guard
from src.assessment.claim_guard import (
    REWRITE_FLAG_ENV,
    SUBJECT_GATE_ENV,
    apply_subject_gate,
    guard_claim,
    question_subject_nations,
)
from src.assessment.situation_store import RevisionRow
from src.synthesis.grounded.estimate import HypothesisScore, KeyJudgment

QUESTION = "架空国の国家アクターは日本の重要インフラに対する事前配置を進めているか"
OLD_CLAIM = "日本の重要インフラへの事前配置は悪化している"
NEW_CLAIM = "日本の重要インフラへの事前配置は横ばいで、新たな悪化の根拠は無い"


@pytest.fixture(autouse=True)
def _subject_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """関門の挙動を確かめるテストは on で回す (既定 shadow の検証は個別に env を消す)。"""
    monkeypatch.setenv(SUBJECT_GATE_ENV, "on")


def _prev(claim: str = OLD_CLAIM) -> RevisionRow:
    return RevisionRow(
        situation_id="s-1",
        rev=3,
        claim=claim,
        claim_type="structural",
        leading_hypothesis="trend_worsening",
        confidence="moderate",
        confidence_basis="",
        hypotheses_json="[]",
        assumptions_json="[]",
        missing_json="[]",
        indicators_json="[]",
        implication="",
        delta_type="no_change",
        delta_note="",
        created_at="2026-10-01T00:00:00+00:00",
    )


def _j(claim: str = OLD_CLAIM, leading: str = "trend_flat") -> KeyJudgment:
    return KeyJudgment(
        id="s-1",
        claim=claim,
        domain="cyber_incident",
        leading_hypothesis=leading,
        confidence="moderate",
        confidence_basis="",
        hypotheses=(
            HypothesisScore(hypothesis=leading, consistent=3, inconsistent=0, verdict="leading"),
        ),
        evidence=(),
    )


def _llm(claim: str | Exception) -> Any:
    llm = AsyncMock()
    if isinstance(claim, Exception):
        llm.generate_structured.side_effect = claim
    else:
        llm.generate_structured.return_value = claim_guard._WireClaim(claim=claim)
    return llm


async def _guard(llm: Any, j: KeyJudgment, *, flip: bool = True, question: str = QUESTION) -> Any:
    return await guard_claim(
        llm=llm, j=j, prev=_prev(), question=question, title="タイトル", flip_confirmed=flip
    )


@pytest.mark.asyncio
async def test_rewrites_claim_when_flip_confirmed_and_claim_unchanged() -> None:
    llm = _llm(NEW_CLAIM)

    res = await _guard(llm, _j())

    assert res.judgment.claim == NEW_CLAIM
    assert llm.generate_structured.await_count == 1
    assert any("書き直し" in n for n in res.notes)


@pytest.mark.asyncio
async def test_does_not_rewrite_while_flip_is_pending() -> None:
    llm = _llm(NEW_CLAIM)

    res = await _guard(llm, _j(), flip=False)

    assert res.judgment.claim == OLD_CLAIM
    llm.generate_structured.assert_not_awaited()


@pytest.mark.asyncio
async def test_keeps_claim_when_rewrite_fails() -> None:
    res = await _guard(_llm(RuntimeError("boom")), _j())

    assert res.judgment.claim == OLD_CLAIM
    assert res.notes == ()


@pytest.mark.asyncio
async def test_keeps_claim_when_rewrite_is_empty() -> None:
    res = await _guard(_llm(""), _j())

    assert res.judgment.claim == OLD_CLAIM


@pytest.mark.asyncio
async def test_flag_off_skips_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REWRITE_FLAG_ENV, "0")
    llm = _llm(NEW_CLAIM)

    res = await _guard(llm, _j())

    assert res.judgment.claim == OLD_CLAIM
    llm.generate_structured.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_rewrite_when_llm_already_revised_claim() -> None:
    llm = _llm("別の文")

    res = await _guard(llm, _j(claim=NEW_CLAIM))

    assert res.judgment.claim == NEW_CLAIM
    llm.generate_structured.assert_not_awaited()


@pytest.mark.asyncio
async def test_rewrite_that_drops_subject_is_discarded() -> None:
    res = await _guard(_llm("台湾では事前配置が横ばいである"), _j())

    assert res.judgment.claim == OLD_CLAIM
    assert any("前回を保持" in n for n in res.notes)


def test_subject_gate_holds_previous_claim_when_country_dropped() -> None:
    j, note = apply_subject_gate(
        _j(claim="台湾とアジア各国で活動が活発化している"), _prev(), QUESTION
    )

    assert j.claim == OLD_CLAIM
    assert "前回を保持" in note


def test_subject_gate_passes_claim_mentioning_subject() -> None:
    j, note = apply_subject_gate(_j(claim=NEW_CLAIM), _prev(), QUESTION)

    assert j.claim == NEW_CLAIM
    assert note == ""


def test_subject_gate_ignores_question_without_nation() -> None:
    j, note = apply_subject_gate(_j(claim="何かの文"), _prev(), "常設の問い")

    assert j.claim == "何かの文"
    assert note == ""


def test_subject_gate_ignores_event_without_question() -> None:
    j, note = apply_subject_gate(_j(claim="台湾の件"), _prev(), "")

    assert j.claim == "台湾の件"
    assert note == ""


def test_subject_gate_adopts_first_claim_with_note() -> None:
    j, note = apply_subject_gate(_j(claim="台湾の件"), None, QUESTION)

    assert j.claim == "台湾の件"
    assert "初回" in note


def test_non_japan_question_uses_its_own_nations() -> None:
    assert question_subject_nations("ロシアは台湾を標的にしているか") >= {"TW"}
    assert question_subject_nations(QUESTION) == frozenset({"JP"})


def test_subject_gate_defaults_to_shadow_and_keeps_new_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SUBJECT_GATE_ENV, raising=False)

    j, note = apply_subject_gate(_j(claim="台湾で活動が活発化している"), _prev(), QUESTION)

    assert j.claim == "台湾で活動が活発化している"
    assert "記録のみ" in note


def test_subject_gate_off_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SUBJECT_GATE_ENV, "off")

    j, note = apply_subject_gate(_j(claim="台湾で活動が活発化している"), _prev(), QUESTION)

    assert j.claim == "台湾で活動が活発化している"
    assert note == ""


@pytest.mark.asyncio
async def test_shadow_gate_accepts_rewrite_that_drops_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SUBJECT_GATE_ENV, "shadow")

    res = await _guard(_llm("台湾では事前配置が横ばいである"), _j())

    assert res.judgment.claim == "台湾では事前配置が横ばいである"
    assert any("記録のみ" in n for n in res.notes)


@pytest.mark.parametrize(
    "claim",
    [
        "今回の新着ソースでは侵入の証拠は見られない",
        "本期間の記事は事前配置の兆候を示す",
        "今期は活動が活発化している",
    ],
)
def test_internal_claim_text_detects_ledger_internal_phrases(claim: str) -> None:
    assert claim_guard.internal_claim_text(claim) != ""


@pytest.mark.parametrize(
    "claim",
    [
        "日本の重要インフラへの事前配置の直接証拠は観測されていない",
        OLD_CLAIM,
    ],
)
def test_internal_claim_text_passes_legitimate_answers(claim: str) -> None:
    assert claim_guard.internal_claim_text(claim) == ""


def test_log_internal_claim_only_logs_and_does_not_change_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warned: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(claim_guard._log, "warning", lambda event, **kw: warned.append((event, kw)))
    j = _j(claim="今回の新着ソースでは変化なし")

    claim_guard.log_internal_claim(j)
    claim_guard.log_internal_claim(_j())

    assert [w[0] for w in warned] == ["claim_internal_text"]
    assert j.claim == "今回の新着ソースでは変化なし"


def test_prior_view_drops_internal_claim_for_standing_only_when_requested() -> None:
    from src.assessment.stateful import _prior_view

    dirty = _prev("今回の新着ソースでは変化は見られない")

    reset = _prior_view(dirty, [], reset_internal_claim=True)
    kept = _prior_view(dirty, [], reset_internal_claim=False)
    clean = _prior_view(_prev(), [], reset_internal_claim=True)

    assert reset.claim == claim_guard.CLAIM_RESET_PLACEHOLDER
    assert kept.claim == dirty.claim
    assert clean.claim == OLD_CLAIM


def test_claim_reset_flag_defaults_on_and_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(claim_guard.RESET_FLAG_ENV, raising=False)
    assert claim_guard.claim_reset_enabled() is True
    monkeypatch.setenv(claim_guard.RESET_FLAG_ENV, "0")
    assert claim_guard.claim_reset_enabled() is False
