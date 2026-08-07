"""統一ジョブ制御レジストリ (2026-07-06) のテスト。

seed/load/override/validation/protection の回帰固定。config_store は tmp SQLite。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.scheduler import job_registry as jr


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "jobs.db"


class TestDefaults:
    def test_default_jobs_cover_all_kinds(self) -> None:
        jobs = jr.default_jobs()
        kinds = {j.kind for j in jobs}
        assert kinds == {"pipeline", "bespoke", "reactive"}
        ids = {j.id for j in jobs}
        # 主要ジョブが漏れなく含まれる
        for jid in (
            "direct-rss-fetch",
            "auto-trigger-synthesis",
            "pir-entity-rebuild",
            "daily-maintenance",
            "ransomware-live-ingest",
        ):
            assert jid in ids

    def test_ids_unique(self) -> None:
        ids = [j.id for j in jr.default_jobs()]
        assert len(ids) == len(set(ids))

    def test_schedule_label(self) -> None:
        by_id = {j.id: j for j in jr.default_jobs()}
        assert "分ごと" in by_id["direct-rss-fetch"].schedule_label()
        assert "reactive" in by_id["auto-trigger-synthesis"].schedule_label()
        assert "03:05" in by_id["pir-entity-rebuild"].schedule_label()


class TestSeedAndOverride:
    def test_seed_then_load_matches_defaults(self, db: Path) -> None:
        assert jr.seed_jobs_if_absent(db_path=db) is True
        assert jr.seed_jobs_if_absent(db_path=db) is False  # 冪等
        loaded = {j.id: j for j in jr.load_jobs(db_path=db)}
        defaults = {j.id: j for j in jr.default_jobs()}
        assert set(loaded) == set(defaults)

    def test_enabled_override_persists(self, db: Path) -> None:
        jr.seed_jobs_if_absent(db_path=db)
        jr.set_job_enabled("ransomware-live-ingest", False, db_path=db)
        loaded = {j.id: j for j in jr.load_jobs(db_path=db)}
        assert loaded["ransomware-live-ingest"].enabled is False
        # 他ジョブは不変
        assert loaded["direct-rss-fetch"].enabled is True

    def test_schedule_override_persists_and_keeps_metadata(self, db: Path) -> None:
        jr.seed_jobs_if_absent(db_path=db)
        jr.update_job_schedule("pir-entity-rebuild", {"hour": 4, "minute": 20}, db_path=db)
        j = jr.get_job("pir-entity-rebuild", db_path=db)
        assert j is not None
        assert (j.hour, j.minute) == (4, 20)
        # メタデータ (title/protection) はコード所有で不変
        assert j.protection == "important"
        assert j.title == "PIR タグ夜間 reconcile"

    def test_new_default_job_appears_even_if_db_seeded_old(self, db: Path) -> None:
        # DB に一部 job だけ保存済み → load は default の canonical 集合を返す
        jr.save_jobs([jr.default_jobs()[0]], db_path=db)
        loaded = {j.id for j in jr.load_jobs(db_path=db)}
        assert loaded == {j.id for j in jr.default_jobs()}


class TestValidation:
    def test_interval_floor(self) -> None:
        bad = jr.default_jobs()[0].model_copy(
            update={"schedule_type": "interval", "interval_minutes": 1}
        )
        assert jr.validate_schedule(bad) is not None
        ok = bad.model_copy(update={"interval_minutes": 60})
        assert jr.validate_schedule(ok) is None

    def test_cron_range(self) -> None:
        base = jr.default_jobs()[4]  # morning-brief (cron)
        assert jr.validate_schedule(base.model_copy(update={"hour": 25})) is not None
        assert jr.validate_schedule(base.model_copy(update={"minute": 99})) is not None
        assert jr.validate_schedule(base) is None

    def test_danger_window_note(self) -> None:
        jobs = jr.default_jobs()
        morning = next(j for j in jobs if j.id == "morning-brief")
        # evening-brief (heavy, 毎日 19:30) に重ねる → 動的に警告
        near = morning.model_copy(update={"hour": 19, "minute": 30})
        note = jr.danger_window_note(near, jobs)
        assert note is not None and "夕ブリーフィング" in note
        # 誰とも重ならない時刻 → None
        safe = morning.model_copy(update={"hour": 5, "minute": 0})
        assert jr.danger_window_note(safe, jobs) is None

    def test_danger_windows_dynamic_follow_reschedule(self) -> None:
        jobs = jr.default_jobs()
        base = {(w["hour"], w["minute"]) for w in jr.danger_windows(jobs)}
        assert (19, 30) in base  # evening-brief
        # weekly-status を 22:00 に動かすと危険帯もそこへ追随する
        moved = [
            j.model_copy(update={"hour": 22, "minute": 0})
            if j.id == "weekly-status-synthesis"
            else j
            for j in jobs
        ]
        after = {(w["hour"], w["minute"]) for w in jr.danger_windows(moved)}
        assert (22, 0) in after and (2, 45) not in after

    def test_danger_note_skips_different_weekday(self) -> None:
        jobs = jr.default_jobs()
        recap = next(j for j in jobs if j.id == "weekly-recap")  # mon 02:00
        # 別曜日 (火) の同時刻は同じ暦日に走らない → 警告しない
        tue_same_time = recap.model_copy(
            update={"id": "x", "day_of_week": "tue", "hour": 2, "minute": 0}
        )
        assert jr.danger_window_note(tue_same_time, jobs) is None


class TestDynamicCollectionSuppression:
    """動的収集抑止 (2026-07-07): 固定窓でなく heavy run 区間との重なりで判定。"""

    def test_heavy_active_on_weekly_monthly_daily(self) -> None:
        by_id = {j.id: j for j in jr.default_jobs()}
        weekly = by_id["weekly-recap"]  # mon
        assert jr._heavy_active_on(weekly, weekday="mon", day_of_month=15) is True
        assert jr._heavy_active_on(weekly, weekday="tue", day_of_month=15) is False
        monthly = by_id["monthly-status-synthesis"]  # day=1
        assert jr._heavy_active_on(monthly, weekday="wed", day_of_month=1) is True
        assert jr._heavy_active_on(monthly, weekday="wed", day_of_month=15) is False
        daily = by_id["morning-brief"]  # 毎日
        assert jr._heavy_active_on(daily, weekday="sun", day_of_month=9) is True

    def test_suppressed_when_overlapping_heavy(self, db: Path) -> None:
        jr.seed_jobs_if_absent(db_path=db)
        # morning-brief は毎日 06:30 開始 (8分)。06:30 発火の rss は被る → 抑止
        assert (
            jr.is_collection_suppressed(
                "direct-rss-fetch",
                now_minute_jst=6 * 60 + 30,
                weekday="wed",
                day_of_month=15,
                db_path=db,
            )
            is True
        )

    def test_suppressed_by_pre_margin(self, db: Path) -> None:
        jr.seed_jobs_if_absent(db_path=db)
        # 06:20 発火 (C=12 → 06:32) は morning-brief 06:30 開始に食い込む → 抑止
        assert (
            jr.is_collection_suppressed(
                "direct-rss-fetch",
                now_minute_jst=6 * 60 + 20,
                weekday="wed",
                day_of_month=15,
                db_path=db,
            )
            is True
        )

    def test_not_suppressed_in_gap(self, db: Path) -> None:
        jr.seed_jobs_if_absent(db_path=db)
        # 10:00 は heavy が無い → 抑止しない (隙間では収集が走る)
        assert (
            jr.is_collection_suppressed(
                "direct-rss-fetch",
                now_minute_jst=10 * 60,
                weekday="wed",
                day_of_month=15,
                db_path=db,
            )
            is False
        )

    def test_weekly_heavy_only_suppresses_on_its_day(self, db: Path) -> None:
        jr.seed_jobs_if_absent(db_path=db)
        # weekly-recap は月曜 02:00 開始 → 月曜 02:00 の rss は抑止
        assert (
            jr.is_collection_suppressed(
                "direct-rss-fetch",
                now_minute_jst=2 * 60,
                weekday="mon",
                day_of_month=15,
                db_path=db,
            )
            is True
        )
        # 水曜 02:00 は weekly 群が非 active・daily heavy も無い → 抑止しない
        assert (
            jr.is_collection_suppressed(
                "direct-rss-fetch",
                now_minute_jst=2 * 60,
                weekday="wed",
                day_of_month=15,
                db_path=db,
            )
            is False
        )

    def test_non_collection_job_never_suppressed(self, db: Path) -> None:
        jr.seed_jobs_if_absent(db_path=db)
        # synthesis は respects_analysis_window=False → 抑止対象外
        assert (
            jr.is_collection_suppressed(
                "weekly-status-synthesis",
                now_minute_jst=2 * 60 + 45,
                weekday="mon",
                day_of_month=15,
                db_path=db,
            )
            is False
        )

    def test_heavy_runtime_widths_differ_monthly_longest(self) -> None:
        by_id = {j.id: j for j in jr.default_jobs()}
        monthly = by_id["monthly-status-synthesis"].max_runtime_minutes
        weekly = by_id["weekly-status-synthesis"].max_runtime_minutes
        mitre = by_id["mitre-actor-sync"].max_runtime_minutes
        # 帯幅で差が付く (monthly > weekly > mitre)。全体最長は夜間 deep-review (75分)。
        assert monthly > weekly > mitre
        assert by_id["ledger-deep-review"].max_runtime_minutes == max(
            j.max_runtime_minutes for j in jr.default_jobs()
        )

    def test_danger_windows_expose_runtime_width(self) -> None:
        jobs = jr.default_jobs()
        by_label = {w["label"]: w for w in jr.danger_windows(jobs)}
        assert by_label["月次状況総括"]["max_runtime_minutes"] == 45
        assert by_label["MITRE アクター同期"]["max_runtime_minutes"] == 6

    def test_collection_jobs_flagged(self) -> None:
        by_id = {j.id: j for j in jr.default_jobs()}
        assert by_id["direct-rss-fetch"].respects_analysis_window is True
        assert by_id["web-scraper-watchers"].respects_analysis_window is True
        assert by_id["morning-brief"].respects_analysis_window is False


class TestApplyToScheduler:
    def test_apply_pauses_disabled_and_reschedules(self) -> None:
        calls: list[tuple[str, Any]] = []

        class FakeScheduler:
            def pause(self, *, job_id: str) -> None:
                calls.append(("pause", job_id))

            def update_cron(
                self,
                hour: int,
                minute: int,
                *,
                job_id: str,
                day_of_week: str | None = None,
                day: str | None = None,
            ) -> None:
                calls.append(("cron", (job_id, hour, minute)))

            def update_interval(self, interval_minutes: int, *, job_id: str) -> None:
                calls.append(("interval", (job_id, interval_minutes)))

        jobs = jr.default_jobs()
        # morning-brief を無効 + 時刻変更、reactive はスケジューラ非対象
        jobs = [
            j.model_copy(update={"enabled": False, "hour": 7, "minute": 0})
            if j.id == "morning-brief"
            else j
            for j in jobs
        ]
        jr.apply_job_registry(FakeScheduler(), jobs)
        assert ("pause", "morning-brief") in calls
        assert ("cron", ("morning-brief", 7, 0)) in calls
        # reactive (auto-trigger) は pause/reschedule されない
        assert all(c[1] != "auto-trigger-synthesis" for c in calls if isinstance(c[1], str))
