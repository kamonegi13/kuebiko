"""Phase G1: trend_aggregator のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.digest.trend_aggregator import (
    TREND_PATTERNS,
    aggregate_trends,
)
from src.storage.run_history import (
    ArticleRecord,
    RunHistoryRepository,
    RunRecord,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "trend.db"


def _now() -> datetime:
    return datetime.now(UTC)


def _seed(
    repo: RunHistoryRepository,
    run_id: int,
    *,
    article_id: str,
    title: str,
    feed: str = "Test Feed",
    created_at: datetime | None = None,
) -> None:
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title=title,
            url=f"https://example.com/{article_id}",
            feed_title=feed,
            importance="medium",
            category="apt",
            status="posted",
            posted_channel="brief",
            created_at=created_at or _now(),
        ),
    )


class TestAggregateTrends:
    def test_returns_empty_when_no_articles(self, db_path: Path) -> None:
        RunHistoryRepository(db_path=db_path)
        result = aggregate_trends(db_path=db_path)
        assert result == []

    def test_detects_new_cluster_with_no_baseline(self, db_path: Path) -> None:
        """過去 0 件で今週 3+ 件なら is_new で採用される。"""
        repo = RunHistoryRepository(db_path=db_path)
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        now = _now()
        # APT41 タイトル 3 件、すべて current window 内
        for i in range(3):
            _seed(
                repo,
                run_id,
                article_id=f"a{i}",
                title=f"APT41 campaign in Asia #{i}",
                feed=f"Feed{i}",  # 3 sources
                created_at=now - timedelta(hours=24),
            )
        clusters = aggregate_trends(db_path=db_path, min_count=3, now=now)
        china_cluster = [c for c in clusters if "China APT" in c.name]
        assert china_cluster, "China APT cluster が検出されるべき"
        assert china_cluster[0].is_new is True
        assert china_cluster[0].total_count == 3

    def test_spike_ratio_calculation(self, db_path: Path) -> None:
        """過去 4 週平均 1 件、今週 4 件 → spike_ratio = 4.0。"""
        repo = RunHistoryRepository(db_path=db_path)
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        now = _now()
        # 過去 4 週で 4 件 (各週 1 件) → 4/4 = 1.0/week baseline
        for i in range(4):
            _seed(
                repo,
                run_id,
                article_id=f"base{i}",
                title=f"Lazarus operation {i}",
                feed=f"FeedB{i}",
                created_at=now - timedelta(hours=168 * (i + 2)),  # 2-5 週前
            )
        # 今週 4 件
        for i in range(4):
            _seed(
                repo,
                run_id,
                article_id=f"cur{i}",
                title=f"Lazarus attack {i}",
                feed=f"FeedC{i}",
                created_at=now - timedelta(hours=24),
            )
        clusters = aggregate_trends(
            db_path=db_path,
            lookback_hours=168,
            baseline_weeks=4,
            min_count=3,
            now=now,
        )
        nk_cluster = [c for c in clusters if "North Korea" in c.name]
        assert nk_cluster
        c = nk_cluster[0]
        assert c.total_count == 4
        assert c.baseline_avg == pytest.approx(1.0)
        assert c.spike_ratio == pytest.approx(4.0)
        assert c.is_new is False

    def test_min_count_filter(self, db_path: Path) -> None:
        """min_count 未満は cluster に採用されない。"""
        repo = RunHistoryRepository(db_path=db_path)
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        _seed(repo, run_id, article_id="single", title="APT41 minor incident")
        clusters = aggregate_trends(db_path=db_path, min_count=3)
        assert all("China APT" not in c.name for c in clusters)

    def test_cross_source_detection(self, db_path: Path) -> None:
        """3+ source で言及されたら is_cross_source=True (spike なくても採用)。"""
        repo = RunHistoryRepository(db_path=db_path)
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        now = _now()
        # baseline は同件数 (spike なし) で sources だけ多い
        for i in range(3):
            _seed(
                repo,
                run_id,
                article_id=f"base{i}",
                title=f"Taiwan strait tension {i}",
                feed=f"FeedB{i}",
                created_at=now - timedelta(hours=168 * (i + 2)),
            )
        for i in range(3):
            _seed(
                repo,
                run_id,
                article_id=f"cur{i}",
                title=f"Taiwan tension #{i}",
                feed=f"FeedC{i}",  # 3 sources
                created_at=now - timedelta(hours=24),
            )
        clusters = aggregate_trends(
            db_path=db_path,
            lookback_hours=168,
            baseline_weeks=4,
            min_count=3,
            min_sources_for_cross=3,
            now=now,
        )
        taiwan = [c for c in clusters if "Taiwan" in c.name]
        # spike_ratio = 3 / (3/4) = 4.0、cross_source も True
        assert taiwan
        assert taiwan[0].is_cross_source is True

    def test_articles_capped_per_cluster(self, db_path: Path) -> None:
        repo = RunHistoryRepository(db_path=db_path)
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        for i in range(10):
            _seed(
                repo,
                run_id,
                article_id=f"a{i}",
                title=f"Salt Typhoon activity {i}",
                feed=f"F{i % 3}",
            )
        clusters = aggregate_trends(
            db_path=db_path,
            min_count=3,
            max_articles_per_cluster=5,
        )
        china = [c for c in clusters if "China APT" in c.name]
        assert china
        assert len(china[0].articles) == 5

    def test_pattern_overrides(self, db_path: Path) -> None:
        repo = RunHistoryRepository(db_path=db_path)
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        for i in range(3):
            _seed(
                repo,
                run_id,
                article_id=f"a{i}",
                title=f"custom-topic event {i}",
                feed=f"F{i}",
            )
        clusters = aggregate_trends(
            db_path=db_path,
            patterns={"my-custom": r"custom-topic"},
            min_count=3,
        )
        names = [c.name for c in clusters]
        assert names == ["my-custom"]


class TestTrendPatterns:
    def test_patterns_have_unique_names(self) -> None:
        assert len(TREND_PATTERNS) == len(set(TREND_PATTERNS.keys()))

    def test_all_patterns_are_compilable(self) -> None:
        import re

        for name, pat in TREND_PATTERNS.items():
            try:
                re.compile(pat, re.IGNORECASE)
            except re.error as e:
                pytest.fail(f"pattern '{name}' は無効な regex: {e}")
