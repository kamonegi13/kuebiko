"""src.pipeline.triage_shadow_deferred / _filter_by_triage の後ろ倒し (2026-10-31)。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pipeline.filters import _filter_by_triage
from src.pipeline.triage_shadow_deferred import (
    SHADOW_MIN_USABLE_SECONDS,
    SHADOW_SAFETY_MARGIN_SECONDS,
    remaining_budget_seconds,
    run_deferred_triage_shadow,
)
from src.storage.repo_triage_shadow import TriageShadowRow
from src.tools.article_model import Article
from src.tools.article_triage import TriageDecision

_AMPLE = SHADOW_SAFETY_MARGIN_SECONDS + SHADOW_MIN_USABLE_SECONDS + 100.0


def _article(i: int) -> Article:
    return Article(
        id=f"rss:{i}",
        title="記事",
        url=f"https://example.com/{i}",
        summary_html="<p>概要</p>",
        published=datetime.now(UTC),
        feed_title="Feed",
        feed_url="https://example.com/feed",
    )


def _row(i: int) -> TriageShadowRow:
    return TriageShadowRow(
        article_id=f"rss:{i}",
        url=f"https://example.com/{i}",
        title="記事",
        feed_title="Feed",
        feed_url="https://example.com/feed",
        current_importance="low",
        current_kept=False,
        flat_importance="medium",
        hint_fired=False,
        hint_reasons=(),
        new_kept=True,
    )


def _run(remaining: float | None, repo: Any, decisions: Any = None) -> Any:
    return run_deferred_triage_shadow(
        decisions if decisions is not None else [(_article(1), "low", False)],
        llm=AsyncMock(),
        keep_importance={"high", "medium"},
        think=False,
        relevance_embedder=None,
        relevance_cascade_llm=None,
        repo=repo,
        remaining_seconds=remaining,
    )


@pytest.mark.asyncio
async def test_filter_defers_shadow_without_calling_llm_and_keeps_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "20")
    monkeypatch.setenv("INGEST_RULE_V2", "0")
    llm: Any = AsyncMock()
    llm.generate_structured.return_value = TriageDecision(importance="medium", reason="実害")
    articles = [_article(1), _article(2)]
    deferred: list[tuple[Article, str, bool]] = []

    # Act
    out_deferred = await _filter_by_triage(
        articles, llm, keep_importance={"high", "medium"}, max_keep=5, shadow_defer_out=deferred
    )
    calls_deferred = llm.generate_structured.await_count
    llm.generate_structured.reset_mock()
    out_inline = await _filter_by_triage(
        articles, llm, keep_importance={"high", "medium"}, max_keep=5
    )

    # Assert: 影の LLM 呼び出しは出ない (triage 本体の 2 回だけ)。入力は渡り、出力は同じ
    assert calls_deferred == 2
    assert llm.generate_structured.await_count > 2  # 後ろ倒しなしだと影の呼び出しが加わる
    assert [(a.id, i, k) for a, i, k in deferred] == [
        ("rss:1", "medium", True),
        ("rss:2", "medium", True),
    ]
    assert out_deferred[5] == []
    assert [a.id for a in out_deferred[0]] == [a.id for a in out_inline[0]]
    assert out_deferred[1:5] == out_inline[1:5]


@pytest.mark.asyncio
async def test_skipped_when_remaining_budget_is_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "20")
    repo = MagicMock()
    low = SHADOW_SAFETY_MARGIN_SECONDS + SHADOW_MIN_USABLE_SECONDS - 1.0

    saved = await _run(low, repo)

    assert saved == 0
    repo.record_triage_shadow.assert_not_called()


@pytest.mark.asyncio
async def test_records_rows_when_budget_is_ample(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "20")
    repo = MagicMock()
    fake = AsyncMock(return_value=[_row(1)])
    monkeypatch.setattr("src.pipeline.triage_shadow_deferred.run_triage_shadow", fake)

    saved = await _run(_AMPLE, repo)

    assert saved == 1
    repo.record_triage_shadow.assert_called_once()
    assert fake.await_args is not None
    budget = fake.await_args.kwargs["budget_seconds"]
    assert budget == pytest.approx(_AMPLE - SHADOW_SAFETY_MARGIN_SECONDS)


@pytest.mark.asyncio
async def test_unbounded_budget_runs_with_default_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "20")
    repo = MagicMock()
    fake = AsyncMock(return_value=[_row(1)])
    monkeypatch.setattr("src.pipeline.triage_shadow_deferred.run_triage_shadow", fake)

    saved = await _run(None, repo)

    assert saved == 1
    assert fake.await_args is not None
    assert fake.await_args.kwargs["budget_seconds"] is None


@pytest.mark.asyncio
async def test_timeout_and_errors_do_not_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "20")
    repo = MagicMock()

    async def _slow(*_a: Any, **_k: Any) -> list[TriageShadowRow]:
        await asyncio.sleep(5)
        return [_row(1)]

    monkeypatch.setattr("src.pipeline.triage_shadow_deferred.run_triage_shadow", _slow)
    monkeypatch.setattr("src.pipeline.triage_shadow_deferred.SHADOW_MIN_USABLE_SECONDS", 0.0)
    monkeypatch.setattr("src.pipeline.triage_shadow_deferred.SHADOW_SAFETY_MARGIN_SECONDS", 0.0)
    assert await _run(0.05, repo) == 0

    monkeypatch.setattr(
        "src.pipeline.triage_shadow_deferred.run_triage_shadow",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    assert await _run(_AMPLE, repo) == 0
    repo.record_triage_shadow.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_or_no_repo_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "0")
    assert await _run(_AMPLE, MagicMock()) == 0
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "20")
    assert await _run(_AMPLE, None) == 0


def test_remaining_budget_none_means_unbounded() -> None:
    assert remaining_budget_seconds(None) is None
