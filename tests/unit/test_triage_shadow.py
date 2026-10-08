"""src.tools.triage_shadow のテスト (2026-10-08、M4)。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from src.tools.article_model import Article
from src.tools.article_triage import TriageDecision
from src.tools.triage_shadow import run_triage_shadow, shadow_sample_size


def _article(i: int, title: str = "記事") -> Article:
    return Article(
        id=f"rss:{i}",
        title=title,
        url=f"https://example.com/{i}",
        summary_html="<p>概要</p>",
        author=None,
        published=datetime.now(UTC),
        feed_title="Feed",
        feed_url="https://example.com/feed",
    )


def test_shadow_sample_size_defaults_to_20(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRIAGE_SHADOW_PER_RUN", raising=False)
    assert shadow_sample_size() == 20


def test_shadow_sample_size_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "0")
    assert shadow_sample_size() == 0


def test_shadow_sample_size_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "not-a-number")
    assert shadow_sample_size() == 20


@pytest.mark.asyncio
async def test_disabled_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "0")
    llm = AsyncMock()
    rows = await run_triage_shadow(
        [(_article(1), "low", False)], llm=llm, keep_importance={"high", "medium"}
    )
    assert rows == []
    llm.generate_structured.assert_not_called()


@pytest.mark.asyncio
async def test_builds_row_with_new_kept_from_flat_importance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "5")
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="medium", reason="平たい判定で medium")
    )
    rows = await run_triage_shadow(
        [(_article(1, "無関係な記事"), "low", False)],
        llm=llm,
        keep_importance={"high", "medium"},
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.article_id == "rss:1"
    assert row.current_importance == "low"
    assert row.current_kept is False
    assert row.flat_importance == "medium"
    assert row.new_kept is True  # flat importance が keep_importance 内


@pytest.mark.asyncio
async def test_new_kept_true_when_hint_fires_even_if_flat_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "5")
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="low", reason="平たい判定は low")
    )
    rows = await run_triage_shadow(
        [(_article(1, "日本の重要インフラ企業への攻撃"), "low", False)],
        llm=llm,
        keep_importance={"high", "medium"},
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.flat_importance == "low"
    assert row.hint_fired is True
    assert "jp" in row.hint_reasons
    assert row.new_kept is True


@pytest.mark.asyncio
async def test_sample_capped_at_per_run_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "2")
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(return_value=TriageDecision(importance="low", reason=""))
    decisions = [(_article(i), "low", False) for i in range(5)]
    rows = await run_triage_shadow(decisions, llm=llm, keep_importance={"high", "medium"})
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_single_article_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "5")
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(side_effect=RuntimeError("llm down"))
    rows = await run_triage_shadow(
        [(_article(1), "low", False)], llm=llm, keep_importance={"high", "medium"}
    )
    # triage_flat 自体が fail-open で medium を返すため、ここでは例外は飲まれて行は作られる
    assert len(rows) == 1
    assert rows[0].flat_importance == "medium"
