"""サイバー政策の救済 (平たい triage が落とした政策系の記事を拾う段) のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.pipeline.policy_rescue import POLICY_RESCUE_MARK, TopicDecision, rescue_cyber_policy
from src.tools.article_model import Article


def _article(i: str, title: str) -> Article:
    return Article(
        id=i,
        url=f"https://example.com/{i}",
        title=title,
        summary_html="",
        published=datetime(2026, 10, 10, tzinfo=UTC),
        feed_title="Feed",
        feed_url="https://example.com/feed",
    )


def _llm(topics: dict[str, str]) -> Any:
    llm = AsyncMock()

    async def _gen(prompt: str, **_: Any) -> TopicDecision:
        for key, topic in topics.items():
            if key in prompt:
                return TopicDecision(topic=topic, reason="r")  # type: ignore[arg-type]
        return TopicDecision(topic="other", reason="r")

    llm.generate_structured.side_effect = _gen
    return llm


@pytest.mark.asyncio
async def test_rescues_low_cyber_policy_as_medium() -> None:
    decisions = [(_article("a", "Securing the Bulk-Power System"), "low", False, "政策")]
    out, rescued = await rescue_cyber_policy(decisions, _llm({"Bulk-Power": "cyber_policy"}))
    assert rescued == {"a"}
    assert out[0][1] == "medium"
    assert out[0][3].startswith(POLICY_RESCUE_MARK)


@pytest.mark.asyncio
async def test_does_not_rescue_noncyber_geopolitics() -> None:
    decisions = [(_article("a", "Navy buys ships"), "low", False, "x")]
    out, rescued = await rescue_cyber_policy(decisions, _llm({"Navy": "noncyber_geopolitics"}))
    assert rescued == set() and out == decisions


@pytest.mark.asyncio
async def test_skips_non_low_and_errors() -> None:
    decisions = [
        (_article("a", "Bulk-Power"), "high", False, ""),
        (_article("b", "Bulk-Power"), "low", True, ""),
    ]
    llm = _llm({"Bulk-Power": "cyber_policy"})
    out, rescued = await rescue_cyber_policy(decisions, llm)
    assert rescued == set() and out == decisions
    llm.generate_structured.assert_not_called()


@pytest.mark.asyncio
async def test_llm_failure_keeps_original_decision() -> None:
    llm = AsyncMock()
    llm.generate_structured.side_effect = RuntimeError("boom")
    decisions = [(_article("a", "Bulk-Power"), "low", False, "")]
    out, rescued = await rescue_cyber_policy(decisions, llm)
    assert rescued == set() and out == decisions


@pytest.mark.asyncio
async def test_shadow_mode_records_but_does_not_change(tmp_path: Any, monkeypatch: Any) -> None:
    import json

    from src.pipeline import policy_rescue

    log = tmp_path / "shadow.jsonl"
    monkeypatch.setattr(policy_rescue, "SHADOW_LOG", log)
    decisions = [(_article("a", "Securing the Bulk-Power System"), "low", False, "政策")]
    out, rescued = await rescue_cyber_policy(
        decisions, _llm({"Bulk-Power": "cyber_policy"}), shadow=True
    )
    assert rescued == set() and out == decisions
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["article_id"] == "a" and row["topic"] == "cyber_policy"


def test_mode_parsing(monkeypatch: Any) -> None:
    from src.pipeline.policy_rescue import policy_rescue_mode

    for raw, want in [("", "off"), ("0", "off"), ("1", "on"), ("shadow", "shadow")]:
        monkeypatch.setenv("CYBER_POLICY_RESCUE", raw)
        assert policy_rescue_mode() == want
