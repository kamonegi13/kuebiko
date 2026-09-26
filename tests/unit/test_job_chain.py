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


@pytest.mark.asyncio
async def test_step_already_running_is_skipped_without_overwriting_its_record() -> None:
    """手動で同じ段が走っていれば、チェーンはその段を飛ばし記録も上書きしない (2026-09-26)。"""
    from datetime import UTC, datetime

    from src.scheduler import job_running

    order: list[str] = []

    async def make(name: str) -> None:
        order.append(name)

    job_running._RUNNING["a"] = datetime.now(UTC)
    try:
        rec = _Recorder()
        result = await run_chain(
            "chain-x",
            [ChainStep("a", lambda: make("a"), 60), ChainStep("b", lambda: make("b"), 60)],
            record=rec,
        )
    finally:
        job_running._RUNNING.pop("a", None)
    assert order == ["b"]
    assert [r[0] for r in rec.rows] == ["b"]
    assert result.skipped == ("a",) and result.ok


@pytest.mark.asyncio
async def test_step_start_is_marked_and_slot_released() -> None:
    """段の開始を mark_start で知らせ、終われば実行枠を返す。"""
    from src.scheduler import job_running

    seen: list[tuple[str, bool]] = []

    async def step() -> None:
        seen.append(("run", job_running.is_running("a")))

    started: list[str] = []
    await run_chain(
        "chain-x", [ChainStep("a", step, 60)], record=_Recorder(), mark_start=started.append
    )
    assert started == ["a"]
    assert seen == [("run", True)]
    assert not job_running.is_running("a")


@pytest.mark.asyncio
async def test_cancelled_step_still_records_so_running_does_not_stick() -> None:
    """中断された段も記録を書く (書かないと mark_start の running が残る — 2026-09-26 レビュー)。"""

    async def hang() -> None:
        await asyncio.sleep(3600)

    rec = _Recorder()
    task = asyncio.create_task(
        run_chain("chain-x", [ChainStep("a", hang, 60)], record=rec, mark_start=lambda _: None)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rec.rows and rec.rows[0][0:2] == ("a", "failed")
