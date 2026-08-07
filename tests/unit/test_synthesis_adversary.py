"""段3 敵対的検証 (対称 red-team) のテスト。apply は決定論・純関数。"""

from __future__ import annotations

from typing import Any

import pytest

from src.synthesis.grounded.adversary import (
    AdversarialResult,
    _WireReview,
    _WireReviews,
    adversarial_review,
    apply_adversarial,
)
from src.synthesis.grounded.estimate import HypothesisScore, KeyJudgment


def _judgment(leading: str = "organized_state_op", conf: str = "high") -> KeyJudgment:
    return KeyJudgment(
        id="j1",
        claim="c",
        domain="cyber_incident",
        leading_hypothesis=leading,
        confidence=conf,  # type: ignore[arg-type]
        confidence_basis="src=high",
        hypotheses=(
            HypothesisScore("organized_state_op", 1, 2, "leading"),
            HypothesisScore("opportunistic_commodity", 3, 0, "viable"),
        ),
        evidence=(),
    )


def test_apply_refutes_caps_low_and_flips() -> None:
    out = apply_adversarial(
        _judgment(),
        AdversarialResult("j1", True, "コモディティが最有力", "opportunistic_commodity"),
    )
    assert out.confidence == "low"
    assert out.leading_hypothesis == "opportunistic_commodity"  # 採点済み → flip
    assert out.adversarial_refuted is True
    assert "コモディティ" in out.adversarial_note


def test_apply_refutes_no_flip_when_recommended_not_scored() -> None:
    out = apply_adversarial(
        _judgment(),
        AdversarialResult("j1", True, "counter", "hacktivism_influence"),  # 未採点
    )
    assert out.confidence == "low"  # 確度は下げる
    assert out.leading_hypothesis == "organized_state_op"  # 未採点仮説には flip しない


def test_apply_not_refuted_records_counter_keeps_conf() -> None:
    out = apply_adversarial(
        _judgment(conf="moderate"), AdversarialResult("j1", False, "弱い別解釈", "")
    )
    assert out.confidence == "moderate"  # 据え置き
    assert out.adversarial_refuted is False
    assert out.adversarial_note == "弱い別解釈"


def test_apply_none_is_noop() -> None:
    j = _judgment()
    assert apply_adversarial(j, None) is j


@pytest.mark.asyncio
async def test_adversarial_review_empty_skips_llm() -> None:
    class FailLLM:
        model = "fake"

        async def generate(self, *a: Any, **k: Any) -> Any:
            raise NotImplementedError

        async def generate_structured(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("should not call LLM for empty judgments")

    out = await adversarial_review(llm=FailLLM(), judgments=())  # type: ignore[arg-type]
    assert out == {}


@pytest.mark.asyncio
async def test_adversarial_review_maps() -> None:
    class FakeLLM:
        model = "fake"

        async def generate(self, *a: Any, **k: Any) -> Any:
            raise NotImplementedError

        async def generate_structured(self, prompt: str, schema: type, **kw: Any) -> Any:
            return _WireReviews(
                reviews=[
                    _WireReview(
                        judgment_id="j1",
                        refutes_leading=True,
                        strongest_counter="x",
                        recommended_leading="opportunistic_commodity",
                    )
                ]
            )

    out = await adversarial_review(llm=FakeLLM(), judgments=(_judgment(),))  # type: ignore[arg-type]
    assert "j1" in out
    assert out["j1"].refutes_leading is True
    assert out["j1"].recommended_leading == "opportunistic_commodity"
