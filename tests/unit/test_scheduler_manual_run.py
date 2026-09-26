"""手動実行 (trigger_now) は 1 回だけ走らせ、定時の予定には触れない (2026-09-26)。

旧実装は次回時刻を今に書き換えていたため、(1) 実行中に押すと同時実行上限で黙って捨てられ、
(2) 一時停止中のジョブ (チェーンの段) に押すと停止が解けて単独で毎時走り出した。
"""

from __future__ import annotations

import asyncio

import pytest

from src.scheduler import job_running
from src.scheduler.job_running import JobBusyError
from src.scheduler.scheduler import BriefingScheduler


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_manual_run_on_paused_job_runs_once_and_stays_paused() -> None:
    calls: list[str] = []

    async def step() -> None:
        calls.append("ran")

    sched = BriefingScheduler(_noop)
    sched.attach_periodic("step-x", step, interval_minutes=60, offset_minutes=0)
    sched.start()
    try:
        sched.pause("step-x")
        sched.trigger_now("step-x")
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.02)
        assert calls == ["ran"]
        assert sched.is_paused("step-x")  # 停止は解けない
    finally:
        sched.shutdown()


@pytest.mark.asyncio
async def test_manual_run_refused_while_running() -> None:
    sched = BriefingScheduler(_noop)
    sched.attach_periodic("step-y", _noop, interval_minutes=60, offset_minutes=0)
    sched.start()
    try:
        async with job_running.running_slot("step-y") as acquired:
            assert acquired
            with pytest.raises(JobBusyError):
                sched.trigger_now("step-y")
    finally:
        sched.shutdown()


@pytest.mark.asyncio
async def test_scheduled_fire_skips_when_same_job_runs() -> None:
    """定時発火と手動・チェーンが重なったら、後から来た方は飛ばす。"""
    calls: list[str] = []

    async def job() -> None:
        calls.append("ran")

    sched = BriefingScheduler(_noop)
    sched.attach_periodic("step-z", job, interval_minutes=60, offset_minutes=0)
    sched.start()
    try:
        registered = sched._scheduler.get_job("step-z")
        assert registered is not None
        async with job_running.running_slot("step-z"):
            await registered.func()
        assert calls == []
    finally:
        sched.shutdown()
