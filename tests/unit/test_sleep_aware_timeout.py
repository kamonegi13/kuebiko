"""スリープ相当の時計の飛びを持ち時間から除く timeout (2026-11-05)。"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.ui.services.sleep_aware_timeout import wait_with_jump_allowance

TICK = 0.02


class _Clock:
    """実時計 + 手で足せる飛び (スリープ復帰後の VM 時計を偽装)。"""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


async def _run(clock: _Clock, work: float, timeout: float, mode: str, jump_at: float | None) -> str:
    async def body() -> str:
        await asyncio.sleep(work)
        return "ok"

    async def jumper() -> None:
        assert jump_at is not None
        await asyncio.sleep(jump_at)
        clock.offset += 5.0  # 5 秒の飛び (閾値 1 秒)

    task = asyncio.ensure_future(body())
    j = asyncio.ensure_future(jumper()) if jump_at is not None else None
    try:
        return await wait_with_jump_allowance(
            task,
            timeout=timeout,
            mode=mode,  # type: ignore[arg-type]
            tick=TICK,
            jump_threshold=1.0,
            clock=clock,
        )
    finally:
        if j:
            j.cancel()


async def test_jump_extends_timeout_when_on() -> None:
    clock = _Clock()
    # 実作業 0.3s、持ち時間 1s。5s の飛びがあっても延長されるので完走する。
    assert await _run(clock, work=0.3, timeout=1.0, mode="on", jump_at=0.1) == "ok"


async def test_jump_kills_without_extension_in_shadow() -> None:
    clock = _Clock()
    with pytest.raises(TimeoutError):
        await _run(clock, work=0.5, timeout=1.0, mode="shadow", jump_at=0.1)


async def test_genuinely_stuck_task_still_times_out_when_on() -> None:
    clock = _Clock()
    with pytest.raises(TimeoutError):
        await _run(clock, work=30.0, timeout=0.2, mode="on", jump_at=None)


async def test_timeout_cancels_task() -> None:
    clock = _Clock()
    cancelled = asyncio.Event()

    async def body() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.ensure_future(body())
    with pytest.raises(TimeoutError):
        await wait_with_jump_allowance(task, timeout=0.1, mode="on", tick=TICK, clock=clock)
    assert cancelled.is_set()
