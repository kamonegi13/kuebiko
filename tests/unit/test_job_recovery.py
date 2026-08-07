"""ジョブ自動リカバリ watchdog (src/scheduler/job_recovery.py) の unit test。

判定は純関数 decide() に集約されている — 全分岐 (healthy/retry/defer/give_up/skip) と
cadence 導出・heavy 帯衝突を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.scheduler.job_recovery import (
    RECOVERY_MAX_ATTEMPTS,
    decide,
    heavy_window_conflict,
    job_cadence,
)
from src.scheduler.job_registry import JobDef

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _daily(job_id: str = "morning-brief", **kw: object) -> JobDef:
    return JobDef(
        id=job_id,
        kind="pipeline",
        title="t",
        description="d",
        schedule_type="cron",
        hour=6,
        minute=30,
        **kw,  # type: ignore[arg-type]
    )


def _weekly(job_id: str = "weekly-status-synthesis") -> JobDef:
    return JobDef(
        id=job_id,
        kind="pipeline",
        title="t",
        description="d",
        schedule_type="cron",
        day_of_week="mon",
        hour=2,
        minute=45,
    )


def _base_kwargs() -> dict[str, object]:
    return {
        "now": NOW,
        "last_success_at": NOW - timedelta(days=2),  # 日次としては stale
        "has_ever_run": True,
        "last_attempt_at": NOW - timedelta(hours=2),
        "attempts_in_window": 0,
        "busy": False,
        "next_fire_at": NOW + timedelta(hours=12),
        "heavy_conflict": False,
    }


class TestCadence:
    def test_daily_weekly_monthly(self) -> None:
        assert job_cadence(_daily()) == "daily"
        assert job_cadence(_weekly()) == "weekly"
        monthly = JobDef(
            id="monthly-status-synthesis",
            kind="pipeline",
            title="t",
            description="d",
            schedule_type="cron",
            day="1",
            hour=3,
            minute=0,
        )
        assert job_cadence(monthly) == "monthly"

    def test_interval_and_reactive_excluded(self) -> None:
        interval = JobDef(
            id="direct-rss-fetch",
            kind="pipeline",
            title="t",
            description="d",
            schedule_type="interval",
            interval_minutes=60,
        )
        assert job_cadence(interval) is None
        reactive = JobDef(
            id="auto-trigger-synthesis",
            kind="reactive",
            title="t",
            description="d",
            schedule_type="reactive",
            debounce_hours=6.0,
        )
        assert job_cadence(reactive) is None


class TestDecide:
    def test_healthy_within_period(self) -> None:
        kw = _base_kwargs() | {"last_success_at": NOW - timedelta(hours=20)}
        assert decide(_daily(), **kw).action == "healthy"  # type: ignore[arg-type]

    def test_stale_triggers_retry(self) -> None:
        d = decide(_daily(), **_base_kwargs())  # type: ignore[arg-type]
        assert d.action == "retry"

    def test_weekly_grace_respected(self) -> None:
        # 7日+6h 以内はまだ healthy (発火予定直後の誤検知防止)
        kw = _base_kwargs() | {"last_success_at": NOW - timedelta(days=7, hours=3)}
        assert decide(_weekly(), **kw).action == "healthy"  # type: ignore[arg-type]
        kw = _base_kwargs() | {"last_success_at": NOW - timedelta(days=7, hours=7)}
        assert decide(_weekly(), **kw).action == "retry"  # type: ignore[arg-type]

    def test_busy_defers(self) -> None:
        kw = _base_kwargs() | {"busy": True}
        assert decide(_daily(), **kw).action == "defer"  # type: ignore[arg-type]

    def test_heavy_conflict_defers(self) -> None:
        kw = _base_kwargs() | {"heavy_conflict": True}
        assert decide(_daily(), **kw).action == "defer"  # type: ignore[arg-type]

    def test_spacing_defers(self) -> None:
        kw = _base_kwargs() | {"last_attempt_at": NOW - timedelta(minutes=10)}
        assert decide(_daily(), **kw).action == "defer"  # type: ignore[arg-type]

    def test_attempt_cap_gives_up(self) -> None:
        kw = _base_kwargs() | {"attempts_in_window": RECOVERY_MAX_ATTEMPTS}
        assert decide(_daily(), **kw).action == "give_up"  # type: ignore[arg-type]

    def test_natural_fire_near_skips(self) -> None:
        kw = _base_kwargs() | {"next_fire_at": NOW + timedelta(minutes=30)}
        assert decide(_daily(), **kw).action == "skip"  # type: ignore[arg-type]

    def test_never_ran_skipped(self) -> None:
        kw = _base_kwargs() | {"has_ever_run": False, "last_success_at": None}
        assert decide(_daily(), **kw).action == "skip"  # type: ignore[arg-type]

    def test_disabled_skipped(self) -> None:
        job = _daily().model_copy(update={"enabled": False})
        assert decide(job, **_base_kwargs()).action == "skip"  # type: ignore[arg-type]

    def test_never_succeeded_but_has_run_retries(self) -> None:
        # 一度走って失敗だけしたジョブ (last_success=None) は回復対象
        kw = _base_kwargs() | {"last_success_at": None}
        assert decide(_daily(), **kw).action == "retry"  # type: ignore[arg-type]


class TestHeavyConflict:
    def test_overlap_with_active_heavy(self) -> None:
        heavy = JobDef(
            id="weekly-status-synthesis",
            kind="pipeline",
            title="t",
            description="d",
            heavy=True,
            max_runtime_minutes=25,
            schedule_type="cron",
            day_of_week="sun",
            hour=12,
            minute=10,
        )
        target = _daily("morning-brief", max_runtime_minutes=8)
        # 2026-07-19 は日曜。12:00 開始の再実行 [12:00,12:08] は heavy [12:10,12:35] と非重複?
        # → 12:08 <= 12:10 なので非衝突。12:05 開始の帯 (h=12:00) なら衝突を確認する
        now_jst = datetime(2026, 7, 19, 12, 5)  # naive JST 相当でよい (hour/minute のみ使用)
        heavy_at_noon = heavy.model_copy(update={"minute": 0})
        assert heavy_window_conflict(target, [target, heavy_at_noon], now_jst) is True

    def test_wrong_weekday_no_conflict(self) -> None:
        heavy = JobDef(
            id="weekly-status-synthesis",
            kind="pipeline",
            title="t",
            description="d",
            heavy=True,
            max_runtime_minutes=25,
            schedule_type="cron",
            day_of_week="mon",
            hour=12,
            minute=0,
        )
        target = _daily("morning-brief")
        now_jst = datetime(2026, 7, 19, 12, 5)  # 日曜 — mon の heavy は非 active
        assert heavy_window_conflict(target, [target, heavy], now_jst) is False
