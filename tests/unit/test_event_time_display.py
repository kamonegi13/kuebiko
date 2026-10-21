"""発生日の粒度の表示 (event_time.event_date_label) と発生時刻の並べ替え・日次絞り込み。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.event_time import event_date_label
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


class TestEventDateLabel:
    def test_day_precision_shows_full_date(self) -> None:
        assert event_date_label("2026-10-19", "occurred", "day") == "2026-10-19"

    def test_month_precision_shows_month_only(self) -> None:
        assert event_date_label("2026-10-01", "occurred", "month") == "2026-10"

    def test_year_precision_shows_year_only(self) -> None:
        assert event_date_label("2026-01-01", "occurred", "year") == "2026"

    def test_relative_precision_marks_approximation(self) -> None:
        assert event_date_label("2026-10-19", "relative", "relative") == "2026-10-19頃"

    def test_unknown_precision_shows_date_as_is(self) -> None:
        assert event_date_label("2026-10-01", "occurred", "unknown") == "2026-10-01"

    def test_reported_basis_has_no_label(self) -> None:
        assert event_date_label("2026-10-19", "reported", "day") is None

    def test_missing_date_has_no_label(self) -> None:
        assert event_date_label(None, None, None) is None
        assert event_date_label("", "occurred", "day") is None


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "ev.db")


def _add(
    repo: RunHistoryRepository,
    article_id: str,
    *,
    created: str,
    event_date: str | None = None,
    basis: str | None = None,
    precision: str | None = None,
) -> None:
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    ts = datetime.fromisoformat(created).replace(tzinfo=UTC)
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title=article_id,
            url=f"https://example.com/{article_id}",
            feed_title="t",
            summary="",
            importance="medium",
            posted_channel="brief",
            status="posted",
            created_at=ts,
            published_at=ts,
            event_date=event_date,
            event_date_basis=basis,
            event_date_precision=precision,
        )
    )


def _ids(rows: list[ArticleRecord]) -> list[str]:
    return [a.article_id for a in rows]


class TestOccurredSortAndFilter:
    def test_sort_occurred_orders_by_event_date_and_keeps_undated_by_report_time(
        self, repo: RunHistoryRepository
    ) -> None:
        # 取込は新しい順 c > b > a だが、発生日は a (10-18) > b (10-10)。
        # c は報道日の写しのみ・d は発生日なし → 報道時刻 (10-15 / 10-12) で置かれ、落ちない。
        _add(
            repo,
            "a",
            created="2026-10-20T00:00:00",
            event_date="2026-10-18",
            basis="occurred",
            precision="day",
        )
        _add(
            repo,
            "b",
            created="2026-10-21T00:00:00",
            event_date="2026-10-10",
            basis="occurred",
            precision="day",
        )
        _add(
            repo,
            "c",
            created="2026-10-15T00:00:00",
            event_date="2026-10-15",
            basis="reported",
            precision="day",
        )
        _add(repo, "d", created="2026-10-12T00:00:00")
        got = _ids(repo.list_articles(status="posted", sort="occurred"))
        assert got == ["a", "c", "d", "b"]
        assert set(got) == {"a", "b", "c", "d"}  # 黙って落とさない

    def test_event_on_filters_to_that_occurred_day_only(self, repo: RunHistoryRepository) -> None:
        _add(
            repo,
            "a",
            created="2026-10-20T00:00:00",
            event_date="2026-10-18",
            basis="occurred",
            precision="day",
        )
        _add(
            repo,
            "b",
            created="2026-10-18T00:00:00",
            event_date="2026-10-18",
            basis="reported",
            precision="day",
        )
        _add(repo, "c", created="2026-10-18T00:00:00")
        _add(
            repo,
            "d",
            created="2026-10-21T00:00:00",
            event_date="2026-10-10",
            basis="occurred",
            precision="day",
        )
        assert _ids(repo.list_articles(status="posted", event_on="2026-10-18")) == ["a"]
