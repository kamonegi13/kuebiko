"""cluster 帯の救済網 — duplicate と確答したときだけ skip する一方向の関門。

⭐ 実測 (2026-09-03): cluster 帯 (cos>=0.78) の 10.2% が別内容 (同一ベンダの
別製品など)。Recall 重視の任務原則により、different / unclear / 判定失敗は
すべて取込へ倒す。
"""

from __future__ import annotations

from typing import cast

import pytest

from src.pipeline.dedup_judge import DuplicateVerdict, is_duplicate
from src.tools.llm_client import LLMClient


class _Llm:
    model = "test"

    def __init__(self, verdict: str | None):
        self._verdict = verdict

    async def generate_structured(self, *a, **k):  # type: ignore[no-untyped-def]
        if self._verdict is None:
            raise RuntimeError("落ちた")
        return DuplicateVerdict(verdict=self._verdict, reason="r")


@pytest.mark.asyncio
async def test_confirmed_duplicate_allows_the_skip() -> None:
    got = await is_duplicate(
        cast(LLMClient, _Llm("duplicate")),
        skipped_title="同じ記事",
        skipped_feed="媒体",
        matched_title="同じ記事",
    )
    assert got is True


@pytest.mark.asyncio
async def test_different_content_is_rescued() -> None:
    """同一ベンダの別製品 → different → 取込へ (実測の主因 class)。"""
    got = await is_duplicate(
        cast(LLMClient, _Llm("different")),
        skipped_title="Oracle WebLogic の脆弱性",
        skipped_feed="媒体",
        matched_title="Oracle HTTP Server の脆弱性",
    )
    assert got is False


@pytest.mark.asyncio
async def test_unclear_and_failure_fall_toward_ingestion() -> None:
    """判定できない・判定が落ちた → None (呼び手は取込へ倒す。Recall 優先)。"""
    assert (
        await is_duplicate(
            cast(LLMClient, _Llm("unclear")),
            skipped_title="a",
            skipped_feed="f",
            matched_title="b",
        )
        is None
    )
    assert (
        await is_duplicate(
            cast(LLMClient, _Llm(None)),
            skipped_title="a",
            skipped_feed="f",
            matched_title="b",
        )
        is None
    )
