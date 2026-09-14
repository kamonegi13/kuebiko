"""毎時ジョブのチェーン実行 (2026-09-15、ジョブ見直し B)。

不変条件:
1. 段は宣言順に直列実行し、1 段の失敗・timeout は隔離して次段へ進む (チェーン全体は止めない)
2. 段ごとに (job_id, status, elapsed) を記録する — 失敗も timeout も記録に残る
3. 段の timeout は段自身の max_runtime を使う
4. 結果は失敗段の一覧を持ち、全段成功なら ok
"""

from __future__ import annotations

import asyncio

import pytest

from src.scheduler.job_chain import ChainStep, run_chain


class _Recorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def __call__(self, job_id: str, status: str, detail: str) -> None:
        self.rows.append((job_id, status, detail))


@pytest.mark.asyncio
async def test_steps_run_in_declared_order_and_all_recorded() -> None:
    order: list[str] = []

    async def make(name: str) -> None:
        order.append(name)

    steps = [ChainStep("a", lambda: make("a"), 60), ChainStep("b", lambda: make("b"), 60)]
    rec = _Recorder()
    result = await run_chain("chain-x", steps, record=rec)
    assert order == ["a", "b"]
    assert [r[0] for r in rec.rows] == ["a", "b"]
    assert all(r[1] == "succeeded" for r in rec.rows)
    assert result.ok and result.failed == ()


@pytest.mark.asyncio
async def test_failed_step_is_isolated_and_chain_continues() -> None:
    order: list[str] = []

    async def boom() -> None:
        order.append("a")
        raise RuntimeError("x")

    async def ok() -> None:
        order.append("b")

    steps = [ChainStep("a", boom, 60), ChainStep("b", ok, 60)]
    rec = _Recorder()
    result = await run_chain("chain-x", steps, record=rec)
    assert order == ["a", "b"]  # a の失敗で b は止まらない
    assert rec.rows[0][0:2] == ("a", "failed") and "RuntimeError" in rec.rows[0][2]
    assert rec.rows[1][0:2] == ("b", "succeeded")
    assert not result.ok and result.failed == ("a",)


@pytest.mark.asyncio
async def test_step_timeout_uses_its_own_budget_and_continues() -> None:
    order: list[str] = []

    async def slow() -> None:
        await asyncio.sleep(5)

    async def ok() -> None:
        order.append("b")

    steps = [ChainStep("a", slow, 0.05), ChainStep("b", ok, 60)]
    rec = _Recorder()
    result = await run_chain("chain-x", steps, record=rec)
    assert order == ["b"]
    assert rec.rows[0][0:2] == ("a", "failed") and "timeout" in rec.rows[0][2]
    assert result.failed == ("a",)


@pytest.mark.asyncio
async def test_elapsed_is_recorded_in_detail() -> None:
    async def ok() -> None:
        return None

    rec = _Recorder()
    await run_chain("chain-x", [ChainStep("a", ok, 60)], record=rec)
    assert "elapsed=" in rec.rows[0][2] and "chain=chain-x" in rec.rows[0][2]
