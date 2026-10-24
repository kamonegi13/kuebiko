"""Grok 投稿の要約の裏付け関門の配線 (briefing._ensure_grounded_summary) のテスト。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from src.pipeline import briefing
from src.pipeline.briefing import UngroundedSummaryError, _ensure_grounded_summary
from src.pipeline.summary import SummaryOutput
from src.tools.article_model import Article

_BODY = "The Great VM Escape: ESXi Exploitation in the Wild www.huntress.com/blog/esxi"
_BAD = "ESXi 7.0.2 の修正版 7.0.2u4 が 2023 年 11 月 22 日に公開された。"
_GOOD = "Huntress が ESXi の仮想マシン脱出の悪用を報告した。"


def _summary(text: str) -> SummaryOutput:
    return SummaryOutput(
        title_ja="見出し",
        importance="medium",
        category="vulnerability",
        summary=text,
        article_type="breaking",
    )


def _article() -> Article:
    return Article(
        id="grok:a:1#2",
        title="ESXi VM Escape",
        url="https://x.com/a/status/2",
        summary_html="",
        published=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
        feed_title="@a",
        feed_url="https://x.com/a",
    )


def _run(summary: SummaryOutput, retries: list[SummaryOutput], body_source: str = "grok") -> Any:
    calls: list[int] = []

    async def fake_summarize(*_a: Any, **_k: Any) -> SummaryOutput:
        calls.append(1)
        return retries[len(calls) - 1]

    original = briefing._summarize_without_runaway
    briefing._summarize_without_runaway = fake_summarize
    try:
        result = asyncio.run(
            _ensure_grounded_summary(
                summary,
                object(),  # type: ignore[arg-type]
                "prompt",
                article=_article(),
                body=_BODY,
                think=False,
                body_source=body_source,
            )
        )
    finally:
        briefing._summarize_without_runaway = original
    return result, len(calls)


def test_default_shadow_keeps_bad_summary_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROK_SUMMARY_GROUNDING", raising=False)
    bad = _summary(_BAD)

    result, retries = _run(bad, [])

    assert result is bad
    assert retries == 0


def test_on_mode_retries_and_uses_grounded_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROK_SUMMARY_GROUNDING", "on")
    good = _summary(_GOOD)

    result, retries = _run(_summary(_BAD), [good])

    assert result is good
    assert retries == 1


def test_on_mode_raises_when_retry_is_still_ungrounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROK_SUMMARY_GROUNDING", "on")

    with pytest.raises(UngroundedSummaryError):
        _run(_summary(_BAD), [_summary(_BAD)])


def test_non_grok_articles_are_never_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROK_SUMMARY_GROUNDING", "on")
    bad = _summary(_BAD)

    result, retries = _run(bad, [], body_source="full_extract")

    assert result is bad
    assert retries == 0


def test_grounded_summary_passes_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROK_SUMMARY_GROUNDING", "on")
    good = _summary(_GOOD)

    result, retries = _run(good, [])

    assert result is good
    assert retries == 0
