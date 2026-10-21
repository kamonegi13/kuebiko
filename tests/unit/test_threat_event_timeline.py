"""アクター活動タイムライン: 発生時刻モードで発生日の無い記事を落とさない・日次絞り込み。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.services.threat_operations import _undated_daily_series, fetch_actor_detail

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


class TestUndatedSeries:
    def test_non_occurred_rows_are_placed_by_report_time(self) -> None:
        rows = [
            {
                "event_date": None,
                "event_date_basis": None,
                "created_at": "2026-10-19T03:00:00+00:00",
                "published_at": "2026-10-19T03:00:00+00:00",
            },
            {
                "event_date": "2026-10-19",
                "event_date_basis": "reported",
                "created_at": "2026-10-19T04:00:00+00:00",
                "published_at": "2026-10-19T04:00:00+00:00",
            },
            {
                "event_date": "2026-10-01",
                "event_date_basis": "occurred",
                "event_date_precision": "month",
                "created_at": "2026-10-18T04:00:00+00:00",
                "published_at": "2026-10-18T04:00:00+00:00",
            },
            {
                "event_date": "2026-10-10",
                "event_date_basis": "occurred",
                "event_date_precision": "day",
                "created_at": "2026-10-18T04:00:00+00:00",
                "published_at": "2026-10-18T04:00:00+00:00",
            },
        ]
        series = dict(_undated_daily_series(rows, lookback_days=30, now=NOW))  # type: ignore[arg-type]
        assert series["2026-10-19"] == 2  # 発生日なし + 報道日のみ
        assert series["2026-10-18"] == 1  # 月のみ (日に置けない) / 日付ありの 1 件は含めない
        assert sum(series.values()) == 3


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "te.db")


def _add(repo: RunHistoryRepository, aid: str, hours_ago: float, **ev: str | None) -> None:
    run_id = repo.start_run(RunRecord(started_at=NOW, pipeline="x", dry_run=False))
    ts = datetime.now(UTC) - timedelta(hours=hours_ago)
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=aid,
            title="Salt Typhoon activity " + aid,
            url=f"https://example.com/{aid}",
            feed_title="t",
            summary="",
            importance="medium",
            posted_channel="brief",
            status="posted",
            created_at=ts,
            published_at=ts,
            **ev,  # type: ignore[arg-type]
        )
    )


class TestActorDetailEventMode:
    def test_recent_articles_carry_event_label_and_state(
        self, repo: RunHistoryRepository, tmp_path: Path
    ) -> None:
        _add(
            repo,
            "m",
            2,
            event_date="2026-10-01",
            event_date_basis="occurred",
            event_date_precision="month",
        )
        _add(repo, "n", 3)
        d = fetch_actor_detail("salt_typhoon", lookback_days=30, db_path=tmp_path / "te.db")
        assert d is not None
        by_id = {a.article_id: a for a in d.recent_articles}
        assert by_id["m"].event_state == "month_only" and by_id["m"].event_date_label == "2026-10"
        assert by_id["n"].event_state == "unknown" and by_id["n"].event_date_label is None
        # 月のみは被覆に数えられる (precision 列を読んでいること)
        assert d.timeline_coverage["month_only"] == 1
        assert sum(c for _, c in d.timeline_event_undated) == 2

    def test_day_filter_event_basis_selects_occurred_day(
        self, repo: RunHistoryRepository, tmp_path: Path
    ) -> None:
        _add(
            repo,
            "a",
            30,
            event_date="2026-10-10",
            event_date_basis="occurred",
            event_date_precision="day",
        )
        _add(
            repo,
            "b",
            2,
            event_date="2026-10-19",
            event_date_basis="occurred",
            event_date_precision="day",
        )
        d = fetch_actor_detail(
            "salt_typhoon",
            lookback_days=30,
            db_path=tmp_path / "te.db",
            day="2026-10-10",
            day_basis="event",
        )
        assert d is not None
        assert [a.article_id for a in d.recent_articles] == ["a"]

    def test_day_filter_undated_basis_selects_undated_by_report_day(
        self, repo: RunHistoryRepository, tmp_path: Path
    ) -> None:
        _add(repo, "u", 2)
        _add(
            repo,
            "a",
            2,
            event_date="2026-10-19",
            event_date_basis="occurred",
            event_date_precision="day",
        )
        day = (
            (datetime.now(UTC) - timedelta(hours=2))
            .astimezone(ZoneInfo("Asia/Tokyo"))
            .strftime("%Y-%m-%d")
        )
        d = fetch_actor_detail(
            "salt_typhoon",
            lookback_days=30,
            db_path=tmp_path / "te.db",
            day=day,
            day_basis="undated",
        )
        assert d is not None
        assert [a.article_id for a in d.recent_articles] == ["u"]
