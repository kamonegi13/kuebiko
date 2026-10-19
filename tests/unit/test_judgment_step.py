"""判断の分類器の専用 step (JUDGMENT_CLASSIFY、2026-10-18)。

今の本番 (s23i) の挙動は不変: 解決先のモデルも付く印 ([task: summary]) も要約と同じ。
新しい印 [task: judgment] は s24b 系のモデルだけに付く。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from src.tools.model_tiers import STEP_INHERITS, Step
from src.tools.task_prefix import (
    PREFIX_TRAINED_MODELS,
    TaskPrefixClient,
    client_for_step,
    prefix_for,
    with_task_prefix,
)


class _Out(BaseModel):
    pass


S24B_TAGS = ["kuebiko-sft:s24b", "kuebiko-sft:s24bi50", "kuebiko-sft:s24b-tmp"]


def test_inherits_article_summary() -> None:
    assert STEP_INHERITS[Step.JUDGMENT_CLASSIFY] is Step.ARTICLE_SUMMARY


@pytest.mark.parametrize("model", sorted(PREFIX_TRAINED_MODELS))
def test_existing_models_keep_summary_marker(model: str) -> None:
    assert (
        with_task_prefix("本文", Step.JUDGMENT_CLASSIFY, model)
        == with_task_prefix("本文", Step.ARTICLE_SUMMARY, model)
        == "[task: summary]\n本文"
    )


def test_s23i_gets_summary_marker_exactly() -> None:
    out = with_task_prefix("P", Step.JUDGMENT_CLASSIFY, "kuebiko-sft:s23i")
    assert out == "[task: summary]\nP"


@pytest.mark.parametrize("model", S24B_TAGS)
def test_s24b_gets_judgment_marker(model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", model)
    assert with_task_prefix("P", Step.JUDGMENT_CLASSIFY, model) == "[task: judgment]\nP"
    # 他の step は s24b でも既定の印のまま
    assert with_task_prefix("P", Step.ARTICLE_SUMMARY, model) == "[task: summary]\nP"
    assert prefix_for(Step.JUDGMENT_CLASSIFY, model) == "[task: judgment]\n"


def test_s24_non_b_models_do_not_get_judgment_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    for model in ("kuebiko-sft:s24", "kuebiko-sft:s24i50"):
        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", model)
        assert with_task_prefix("P", Step.JUDGMENT_CLASSIFY, model) == "[task: summary]\nP"


def test_default_prefix_for_is_summary() -> None:
    assert prefix_for(Step.JUDGMENT_CLASSIFY) == "[task: summary]\n"


def test_untagged_model_gets_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SFT_TASK_PREFIX_MODELS", raising=False)
    assert with_task_prefix("P", Step.JUDGMENT_CLASSIFY, "kuebiko-sft:n17c") == "P"


def test_client_marker_follows_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", "kuebiko-sft:s24b")
    seen: list[str] = []

    class _Inner:
        def __init__(self, model: str) -> None:
            self.model = model

        async def generate_structured(self, prompt: str, schema: Any, **kw: Any) -> str:
            seen.append(prompt)
            return "ok"

    import asyncio

    for model in ("kuebiko-sft:s23i", "kuebiko-sft:s24b"):
        base: Any = TaskPrefixClient(_Inner(model), Step.ARTICLE_SUMMARY)  # type: ignore[arg-type]
        judge = client_for_step(base, Step.JUDGMENT_CLASSIFY)
        asyncio.run(judge.generate_structured("PROMPT", _Out))
    assert seen == ["[task: summary]\nPROMPT", "[task: judgment]\nPROMPT"]
