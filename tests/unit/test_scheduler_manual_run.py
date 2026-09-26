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


@pytest.mark.asyncio
async def test_pipeline_job_holds_slot_until_run_completes_and_manual_flag_passes() -> None:
    """pipeline は完了まで実行枠を持つ (起動だけで戻るとチェーンと続けて 2 本走った)。
    手動実行では manual=True が runner に届く (重い帯の収集抑止を受けない)。"""
    from src.scheduler.scheduler import ScheduledPipeline

    release = asyncio.Event()
    seen: list[bool] = []

    async def runner(name: str, *, manual: bool = False) -> None:
        seen.append(manual)
        await release.wait()

    sched = BriefingScheduler.from_pipelines(
        runner, [ScheduledPipeline(name="rss-x", hour=6, minute=0)]
    )
    sched.start()
    try:
        sched.trigger_now("rss-x")
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.02)
        assert seen == [True]
        assert job_running.is_running("rss-x")  # run が終わるまで枠を持つ
        with pytest.raises(JobBusyError):
            sched.trigger_now("rss-x")
        release.set()
        for _ in range(50):
            if not job_running.is_running("rss-x"):
                break
            await asyncio.sleep(0.02)
        assert not job_running.is_running("rss-x")
    finally:
        release.set()
        sched.shutdown()


@pytest.mark.asyncio
async def test_recovery_trigger_is_distinguished_from_manual() -> None:
    """自動復旧 (watchdog) の起動は手動と区別して runner に届く (2026-09-27)。

    従来は watchdog も手動も triggered_by='manual' (その前は 'scheduler') で記録され、
    実行履歴から復旧が働いたかを追えなかった。
    """
    from src.scheduler.scheduler import ScheduledPipeline

    seen: list[tuple[bool, str]] = []

    async def runner(name: str, *, manual: bool = False, trigger: str = "manual") -> None:
        seen.append((manual, trigger))

    sched = BriefingScheduler.from_pipelines(
        runner, [ScheduledPipeline(name="rss-r", hour=6, minute=0)]
    )
    sched.start()
    try:
        sched.trigger_now("rss-r", source="recovery")
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.02)
        assert seen == [(True, "recovery")]
    finally:
        sched.shutdown()
