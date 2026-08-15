"""APScheduler ラッパ (Phase 1.5 → Phase 2.7 で多 pipeline 対応に拡張)。

毎朝指定時刻 (TZ=Asia/Tokyo、デフォルト 06:30 JST) に複数 pipeline を
それぞれ独立した cron job として登録する。各 job 定義は
``config/pipelines.yaml`` の ``pipelines[].schedule`` から取得する。

Mac スリープ復帰時のジョブ取りこぼし対策:
- ``coalesce=True``: 連続 misfire 時に 1 回だけ実行 (重複投稿防止)
- ``misfire_grace_time=3600``: 1 時間以内のずれは許容してキャッチアップ実行

設計方針:
- AsyncIOScheduler を使い FastAPI のイベントループに統合
- ジョブ関数は UI からの「即時実行」と同一のコードパスを通る
  (= UI 経由の手動実行と cron 起動が完全に等価)
- pipeline 単位に on/off + 時刻設定を yaml で行う
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_TIMEZONE = "Asia/Tokyo"
# Phase 2.6a: Grok のタスクが 06:00 JST 完了想定 → 06:30 JST 起動
# (タスク完了 + メール配信のリードタイムを確保してから取得開始)。
DEFAULT_HOUR = 6
DEFAULT_MINUTE = 30
DEFAULT_MISFIRE_GRACE_SECONDS = 3600  # 1 時間

# 後方互換: Phase 1.5 時代に書かれた呼び出しが使う daily-briefing job ID
DAILY_JOB_ID = "daily-briefing"

# Phase 5Q-2: watcher の job_id 名前空間プレフィクス。
# Pipeline と watcher を混在管理する際に区別するために使う。
WATCHER_JOB_PREFIX = "watcher:"


def watcher_job_id(watcher_name: str) -> str:
    """``watcher_name`` から APScheduler 内部で使う job_id を組み立てる。"""
    return f"{WATCHER_JOB_PREFIX}{watcher_name}"


# 各 pipeline 用 callback の型: pipeline_name を受け取り 1 回実行する
PipelineRunCallback = Callable[[str], Awaitable[Any]]

# Phase 5Q-2: watcher 用 callback。watcher_name を受け取り 1 回実行する。
# pipeline と分けるのは、watcher が subprocess 経由ではなく直接 async 関数を
# 呼ぶ設計のため (低頻度・軽量、要約 / triage を共有しないため)。
WatcherRunCallback = Callable[[str], Awaitable[Any]]


@dataclass(frozen=True)
class ScheduledPipeline:
    """``BriefingScheduler.start_with_pipelines`` に渡す定義。

    interval_minutes が指定されると IntervalTrigger 優先 (Phase 3.1)、
    それ以外は hour/minute での CronTrigger となる。
    """

    name: str
    hour: int
    minute: int
    enabled: bool = True
    display_name: str | None = None
    # Phase 3.1: 定期実行 (1 日 1 回ではなく N 分おき)
    interval_minutes: int | None = None
    # interval の clock-aligned 起点からのオフセット (分)。同時刻起動の race 回避用。
    interval_offset_minutes: int = 0
    # Phase 5T-J: 週次 / 平日のみ 等 (APScheduler day_of_week 互換、例: "sun")
    day_of_week: str | None = None
    # 段3 (真の月次): 月単位 cron 用 day-of-month (APScheduler 互換、例: "last" / "1")
    day: str | None = None

    @property
    def is_interval(self) -> bool:
        return self.interval_minutes is not None


@dataclass(frozen=True)
class ScheduledWatcher:
    """Phase 5Q-2: APScheduler に登録する watcher 定義。

    Pipeline と異なり cron 1 日 1 回起動のみサポート (interval 不要)。
    Watcher は主パイプライン (RSS / Grok) で扱えないソース
    (例: Project Zero の壊れた feed) を直接処理する。
    """

    name: str
    hour: int
    minute: int
    enabled: bool = True
    display_name: str | None = None


class BriefingScheduler:
    """複数 pipeline 対応の AsyncIOScheduler ラッパ。

    使い方 (推奨、Phase 5A 以降):
        # subprocess 経路を使うことで stdout が run_logs に永続化され、
        # articles テーブルにも 1 ブリーフィング = 1 行が書き込まれる。
        runner_callback = lambda name: runner.start_subprocess_run(
            pipeline_name=name, triggered_by="scheduler", ...,
        )
        scheduler = BriefingScheduler.from_pipelines(runner_callback, pipelines)
        scheduler.start()

    後方互換 (旧 API、Phase 1.5):
        scheduler = BriefingScheduler(job_func)  # 単一 daily-briefing
        scheduler.start()
    """

    def __init__(
        self,
        job_func: Callable[[], Awaitable[Any]] | None = None,
        *,
        timezone: str = DEFAULT_TIMEZONE,
        hour: int = DEFAULT_HOUR,
        minute: int = DEFAULT_MINUTE,
        misfire_grace_time: int = DEFAULT_MISFIRE_GRACE_SECONDS,
    ) -> None:
        self._timezone = timezone
        self._misfire_grace_time = misfire_grace_time
        self._scheduler = AsyncIOScheduler(timezone=timezone)

        # 旧 API: 単一 job 関数を渡されたケース。Phase 1.5 後方互換用。
        self._legacy_job_func = job_func
        self._legacy_hour = hour
        self._legacy_minute = minute

        # Phase 2.7 新 API: pipeline_name → (callback, schedule) のマップ
        self._pipeline_runner: PipelineRunCallback | None = None
        self._scheduled_pipelines: tuple[ScheduledPipeline, ...] = ()

        # Phase 5Q-2: watcher 用 (pipeline と独立管理)
        self._watcher_runner: WatcherRunCallback | None = None
        self._scheduled_watchers: tuple[ScheduledWatcher, ...] = ()

        # 被害状況コレクタ等の bespoke 定期タスク (pipeline/watcher 機構に乗らない interval job)。
        # (job_id, async callback, interval_minutes, offset_minutes) のリスト。
        self._periodic_tasks: list[tuple[str, Callable[[], Awaitable[Any]], int, int]] = []
        # 日次 cron タスク (heartbeat 等)。(job_id, async callback, hour, minute)。
        self._daily_tasks: list[tuple[str, Callable[[], Awaitable[Any]], int, int, str | None]] = []

    # ----- 構築 (Phase 2.7) -----

    @classmethod
    def from_pipelines(
        cls,
        runner: PipelineRunCallback,
        pipelines: list[ScheduledPipeline],
        *,
        timezone: str = DEFAULT_TIMEZONE,
        misfire_grace_time: int = DEFAULT_MISFIRE_GRACE_SECONDS,
    ) -> BriefingScheduler:
        """pipeline 群から多 job スケジューラを組み立てる。"""
        instance = cls(
            timezone=timezone,
            misfire_grace_time=misfire_grace_time,
        )
        instance._pipeline_runner = runner  # noqa: SLF001
        instance._scheduled_pipelines = tuple(pipelines)  # noqa: SLF001
        return instance

    def attach_periodic(
        self,
        job_id: str,
        func: Callable[[], Awaitable[Any]],
        *,
        interval_minutes: int,
        offset_minutes: int = 0,
    ) -> None:
        """bespoke な interval タスクを登録する (pipeline/watcher 機構外)。

        被害状況コレクタ (ransomware.live ingest) のような「source パイプラインに乗らない
        定期処理」用。start() 前後どちらでも呼べる。clock-aligned + offset で :00 race を避ける。
        """
        self._periodic_tasks.append((job_id, func, interval_minutes, offset_minutes))
        if self._scheduler.running:
            self._register_periodic_jobs()

    def attach_daily(
        self,
        job_id: str,
        func: Callable[[], Awaitable[Any]],
        *,
        hour: int,
        minute: int = 0,
        day_of_week: str | None = None,
    ) -> None:
        """bespoke な cron タスクを登録する (heartbeat 等の定時 1 回/日 job)。

        interval と違い固定時刻に発火するため「毎日必ず同じ時刻に届く 1 通」の
        規約 (dead-man's switch) を成立させられる。start() 前後どちらでも呼べる。
        ``day_of_week`` (例 "mon") を渡すと週次になる (fill-rate 監査等)。
        """
        self._daily_tasks.append((job_id, func, hour, minute, day_of_week))
        if self._scheduler.running:
            self._register_daily_jobs()

    def attach_watchers(
        self,
        runner: WatcherRunCallback,
        watchers: list[ScheduledWatcher],
    ) -> None:
        """Phase 5Q-2: watcher 群を後から登録する。

        scheduler.start() の前後どちらでも呼べる。start() 前なら start() 時に
        登録され、start() 後なら即座に APScheduler に追加される。
        """
        self._watcher_runner = runner
        self._scheduled_watchers = tuple(watchers)
        if self._scheduler.running:
            self._register_watcher_jobs()

    # ----- ライフサイクル -----

    def start(self) -> None:
        """スケジューラを起動し、対象 pipeline / watcher ごとに cron job を登録する。"""
        if not self._scheduler.running:
            self._scheduler.start()

        registered_any = False
        if self._pipeline_runner is not None and self._scheduled_pipelines:
            self._register_pipeline_jobs()
            registered_any = True
        elif self._legacy_job_func is not None:
            self._register_legacy_job()
            registered_any = True

        # Phase 5Q-2: watcher 登録 (pipeline と独立、両方とも 0 件でも警告するために最後にチェック)
        if self._watcher_runner is not None and self._scheduled_watchers:
            self._register_watcher_jobs()
            registered_any = True

        # bespoke 定期タスク (被害状況コレクタ等)
        if self._periodic_tasks:
            self._register_periodic_jobs()
            registered_any = True

        # 日次 cron タスク (heartbeat 等)
        if self._daily_tasks:
            self._register_daily_jobs()
            registered_any = True

        if not registered_any:
            _log.warning("scheduler_started_without_jobs")
            return

        _log.info(
            "scheduler_started",
            timezone=self._timezone,
            jobs=[j.id for j in self._scheduler.get_jobs()],
        )

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        _log.info("scheduler_shutdown")

    # ----- job 登録 -----

    def _register_pipeline_jobs(self) -> None:
        assert self._pipeline_runner is not None
        runner = self._pipeline_runner
        for sp in self._scheduled_pipelines:
            if not sp.enabled:
                _log.info("scheduler_pipeline_disabled", pipeline=sp.name)
                continue
            trigger: CronTrigger | IntervalTrigger
            if sp.is_interval:
                # Phase 3.1: N 分おきの定期実行 (clock-aligned)。
                # コンテナ再起動による時刻ずれを防ぐため、UNIX epoch 基準の
                # 整列点 (例: 60 分なら HH:00、30 分なら HH:00/:30) に start_date
                # を合わせる。
                assert sp.interval_minutes is not None
                # clock-aligned + offset で同時刻 race を避ける (update_interval と同一)
                start_date = _aligned_start_with_offset(
                    sp.interval_minutes, sp.interval_offset_minutes, self._timezone
                )
                trigger = IntervalTrigger(
                    minutes=sp.interval_minutes,
                    start_date=start_date,
                    timezone=self._timezone,
                )
                schedule_label = (
                    f"interval:{sp.interval_minutes}min "
                    f"(next aligned: {start_date.strftime('%H:%M')})"
                )
            else:
                cron_kwargs: dict[str, Any] = {
                    "hour": sp.hour,
                    "minute": sp.minute,
                    "timezone": self._timezone,
                }
                if sp.day_of_week:
                    cron_kwargs["day_of_week"] = sp.day_of_week
                if sp.day:
                    cron_kwargs["day"] = sp.day
                trigger = CronTrigger(**cron_kwargs)
                dow_label = f"[{sp.day_of_week}] " if sp.day_of_week else ""
                dom_label = f"[day={sp.day}] " if sp.day else ""
                schedule_label = f"cron:{dom_label}{dow_label}{sp.hour:02d}:{sp.minute:02d}"

            async def _run(name: str = sp.name) -> None:
                await runner(name)

            self._scheduler.add_job(
                _run,
                trigger=trigger,
                id=sp.name,
                name=sp.display_name or f"{sp.name} pipeline",
                coalesce=True,
                misfire_grace_time=self._misfire_grace_time,
                replace_existing=True,
            )
            _log.info(
                "scheduler_job_added",
                job_id=sp.name,
                schedule=schedule_label,
            )

    def _register_watcher_jobs(self) -> None:
        """Phase 5Q-2: watcher 群を APScheduler に追加する。

        watcher は cron 1 日 1 回起動のみで interval をサポートしない。
        job id は "watcher:<name>" として pipeline と名前空間を分ける。
        """
        assert self._watcher_runner is not None
        runner = self._watcher_runner
        for sw in self._scheduled_watchers:
            if not sw.enabled:
                _log.info("scheduler_watcher_disabled", watcher=sw.name)
                continue
            trigger = CronTrigger(
                hour=sw.hour,
                minute=sw.minute,
                timezone=self._timezone,
            )

            async def _run(name: str = sw.name) -> None:
                await runner(name)

            job_id = watcher_job_id(sw.name)
            self._scheduler.add_job(
                _run,
                trigger=trigger,
                id=job_id,
                name=sw.display_name or f"{sw.name} watcher",
                coalesce=True,
                misfire_grace_time=self._misfire_grace_time,
                replace_existing=True,
            )
            _log.info(
                "scheduler_watcher_added",
                job_id=job_id,
                cron=f"{sw.hour:02d}:{sw.minute:02d}",
            )

    def _register_periodic_jobs(self) -> None:
        """bespoke 定期タスク (被害状況コレクタ等) を interval job として登録する。"""
        for job_id, func, interval_minutes, offset_minutes in self._periodic_tasks:
            start_date = _aligned_start_with_offset(
                interval_minutes, offset_minutes, self._timezone
            )
            trigger = IntervalTrigger(
                minutes=interval_minutes,
                start_date=start_date,
                timezone=self._timezone,
            )

            async def _run(f: Callable[[], Awaitable[Any]] = func) -> None:
                await f()

            self._scheduler.add_job(
                _run,
                trigger=trigger,
                id=job_id,
                name=job_id,
                coalesce=True,
                misfire_grace_time=self._misfire_grace_time,
                replace_existing=True,
            )
            _log.info(
                "scheduler_periodic_added",
                job_id=job_id,
                schedule=f"interval:{interval_minutes}min+{offset_minutes}",
            )

    def _register_daily_jobs(self) -> None:
        """日次/週次 cron タスク (heartbeat 等) を CronTrigger で登録する。"""
        for job_id, func, hour, minute, day_of_week in self._daily_tasks:
            trigger = CronTrigger(
                day_of_week=day_of_week,
                hour=hour,
                minute=minute,
                timezone=self._timezone,
            )

            async def _run(f: Callable[[], Awaitable[Any]] = func) -> None:
                await f()

            self._scheduler.add_job(
                _run,
                trigger=trigger,
                id=job_id,
                name=job_id,
                coalesce=True,
                misfire_grace_time=self._misfire_grace_time,
                replace_existing=True,
            )
            dow = f"[{day_of_week}] " if day_of_week else ""
            _log.info(
                "scheduler_daily_added",
                job_id=job_id,
                schedule=f"cron:{dow}{hour:02d}:{minute:02d}",
            )

    def _register_legacy_job(self) -> None:
        """Phase 1.5 時代の単一 daily-briefing 登録 (後方互換)。"""
        assert self._legacy_job_func is not None
        trigger = CronTrigger(
            hour=self._legacy_hour,
            minute=self._legacy_minute,
            timezone=self._timezone,
        )
        self._scheduler.add_job(
            self._legacy_job_func,
            trigger=trigger,
            id=DAILY_JOB_ID,
            name="Daily CTI briefing pipeline",
            coalesce=True,
            misfire_grace_time=self._misfire_grace_time,
            replace_existing=True,
        )
        _log.info(
            "scheduler_job_added",
            job_id=DAILY_JOB_ID,
            cron=f"{self._legacy_hour:02d}:{self._legacy_minute:02d}",
        )

    # ----- introspection -----

    def list_jobs(self) -> list[tuple[str, datetime | None]]:
        """登録済みジョブの (id, next_run_time) 一覧。"""
        return [(j.id, j.next_run_time) for j in self._scheduler.get_jobs()]

    def scheduled_watchers(self) -> tuple[ScheduledWatcher, ...]:
        """Phase 5Q-2: 登録済 watcher 定義の immutable view。"""
        return self._scheduled_watchers

    def next_run_at(self, job_id: str = DAILY_JOB_ID) -> datetime | None:
        job = self._scheduler.get_job(job_id)
        if job is None or job.next_run_time is None:
            return None
        next_run: datetime = job.next_run_time
        return next_run

    def is_paused(self, job_id: str = DAILY_JOB_ID) -> bool:
        job = self._scheduler.get_job(job_id)
        return bool(job is None or job.next_run_time is None)

    # ----- control -----

    def pause(self, job_id: str = DAILY_JOB_ID) -> None:
        self._scheduler.pause_job(job_id)
        _log.info("scheduler_job_paused", job_id=job_id)

    def resume(self, job_id: str = DAILY_JOB_ID) -> None:
        self._scheduler.resume_job(job_id)
        _log.info("scheduler_job_resumed", job_id=job_id)

    def trigger_now(self, job_id: str = DAILY_JOB_ID) -> datetime:
        """次回実行を「今すぐ」に書き換えて手動 trigger する。"""
        run_at = datetime.now(UTC)
        self._scheduler.modify_job(job_id, next_run_time=run_at)
        _log.info("scheduler_job_triggered_manually", job_id=job_id)
        return run_at

    def update_cron(
        self,
        hour: int,
        minute: int,
        *,
        job_id: str = DAILY_JOB_ID,
        day_of_week: str | None = None,
        day: str | None = None,
    ) -> None:
        """cron 時刻を変更して reschedule する (Phase 5T-J: day_of_week / 月次 day 対応)。"""
        cron_kwargs: dict[str, Any] = {
            "hour": hour,
            "minute": minute,
            "timezone": self._timezone,
        }
        if day_of_week:
            cron_kwargs["day_of_week"] = day_of_week
        if day:
            cron_kwargs["day"] = day
        self._scheduler.reschedule_job(
            job_id,
            trigger=CronTrigger(**cron_kwargs),
        )
        if job_id == DAILY_JOB_ID:
            self._legacy_hour = hour
            self._legacy_minute = minute
        dow_label = f"[{day_of_week}] " if day_of_week else ""
        _log.info(
            "scheduler_cron_updated",
            job_id=job_id,
            cron=f"{dow_label}{hour:02d}:{minute:02d}",
        )

    def update_interval(
        self,
        interval_minutes: int,
        *,
        job_id: str = DAILY_JOB_ID,
        offset_minutes: int = 0,
    ) -> None:
        """interval 実行に切替えて reschedule する (Phase 3.1、clock-aligned + offset)。

        offset_minutes は起動時経路 (start) と同じ意味 — clock-aligned (:00 等) から
        ずらして同時刻 race を避ける。2026-08-15 まで本経路が offset を捨てており、
        runtime reschedule で :00 に丸まるバグがあった (DB には offset が残るため
        再起動で直る、という分かりにくい挙動になっていた)。
        """
        start_date = _aligned_start_with_offset(interval_minutes, offset_minutes, self._timezone)
        self._scheduler.reschedule_job(
            job_id,
            trigger=IntervalTrigger(
                minutes=interval_minutes,
                start_date=start_date,
                timezone=self._timezone,
            ),
        )
        _log.info(
            "scheduler_interval_updated",
            job_id=job_id,
            interval_minutes=interval_minutes,
            offset_minutes=offset_minutes,
            next_aligned=start_date.isoformat(),
        )


def _aligned_start_with_offset(
    interval_minutes: int,
    offset_minutes: int,
    timezone: str,
    *,
    now: datetime | None = None,
) -> datetime:
    """clock-aligned 境界 + offset の「次に来る」時刻を返す。

    単純に「次の境界 + offset」とすると、境界直後 (例: 23:05 に 60 分 +20 分を
    設定) は 00:20 まで飛んでしまう。直前境界の offset スロット (23:20) が
    まだ未来ならそちらを使い、最大 1 interval の取りこぼしを防ぐ。
    """
    tz = ZoneInfo(timezone)
    current = now if now is not None else datetime.now(tz)
    start = _next_aligned_time(interval_minutes, timezone, now=current)
    if offset_minutes:
        start = start + timedelta(minutes=offset_minutes)
        earlier = start - timedelta(minutes=interval_minutes)
        if earlier > current:
            start = earlier
    return start


def _next_aligned_time(
    interval_minutes: int, timezone: str, *, now: datetime | None = None
) -> datetime:
    """指定 timezone での次の clock-aligned 時刻を返す (Phase 3.1)。

    UNIX epoch を ``interval_minutes * 60`` 秒で量子化することで、
    例えば 60 分間隔なら HH:00、30 分なら HH:00/:30、15 分なら HH:00/:15/:30/:45
    に揃える。コンテナ再起動の前後でも次回実行時刻がブレない。

    特殊ケース: 現在時刻がちょうど境界上なら次の境界 (interval_minutes 後) を返す。
    """
    tz = ZoneInfo(timezone)
    current = now if now is not None else datetime.now(tz)
    quantum_seconds = interval_minutes * 60
    now_epoch = current.timestamp()
    next_epoch = math.floor(now_epoch / quantum_seconds + 1) * quantum_seconds
    return datetime.fromtimestamp(next_epoch, tz=tz)
