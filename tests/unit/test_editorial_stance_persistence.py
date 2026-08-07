"""Phase B-R5b 観察: editorial_stance の DB 永続化 + analyst review のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from src.storage.run_history import (
    ArticleRecord,
    RunHistoryRepository,
    RunRecord,
)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "es.db")


def _now() -> datetime:
    return datetime.now(UTC)


def _add(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    title: str,
    feed_title: str,
    stance: str | None,
    hours_ago: float = 1.0,
) -> None:
    run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title=title,
            url=f"https://example.com/{article_id}",
            feed_title=feed_title,
            status="posted",
            editorial_stance=stance,
            created_at=_now() - timedelta(hours=hours_ago),
        ),
    )


class TestEditorialStancePersistence:
    def test_stance_round_trip(self, repo: RunHistoryRepository) -> None:
        _add(repo, article_id="a", title="x", feed_title="f", stance="propaganda")
        rows = repo.list_recent_articles_with_stance(lookback_days=2)
        assert len(rows) == 1
        assert rows[0].editorial_stance == "propaganda"

    def test_null_stance_round_trip(self, repo: RunHistoryRepository) -> None:
        _add(repo, article_id="a", title="x", feed_title="f", stance=None)
        rows = repo.list_recent_articles_with_stance(lookback_days=2)
        assert rows[0].editorial_stance is None

    def test_stance_filter(self, repo: RunHistoryRepository) -> None:
        _add(repo, article_id="a", title="x", feed_title="f", stance="factual_report")
        _add(repo, article_id="b", title="y", feed_title="f", stance="propaganda")
        prop_only = repo.list_recent_articles_with_stance(
            stance_filter="propaganda",
            lookback_days=2,
        )
        assert len(prop_only) == 1
        assert prop_only[0].article_id == "b"


class TestCountEditorialStanceByFeed:
    def test_crosstab(self, repo: RunHistoryRepository) -> None:
        _add(repo, article_id="a", title="x", feed_title="Sputnik", stance="propaganda")
        _add(repo, article_id="b", title="y", feed_title="Sputnik", stance="propaganda")
        _add(repo, article_id="c", title="z", feed_title="Sputnik", stance="factual_report")
        _add(repo, article_id="d", title="w", feed_title="Crisis Group", stance="analytical")
        result = repo.count_editorial_stance_by_feed(lookback_days=2)
        by_feed: dict[str, dict[str, int]] = {}
        for row in result:
            by_feed.setdefault(str(row["feed_title"]), {})[str(row["stance"])] = int(
                cast(int, row["n"])
            )
        assert by_feed["Sputnik"]["propaganda"] == 2
        assert by_feed["Sputnik"]["factual_report"] == 1
        assert by_feed["Crisis Group"]["analytical"] == 1

    def test_unknown_for_null_stance(self, repo: RunHistoryRepository) -> None:
        _add(repo, article_id="a", title="x", feed_title="f", stance=None)
        result = repo.count_editorial_stance_by_feed(lookback_days=2)
        # NULL stance は 'unknown' として集計される (COALESCE)
        assert result[0]["stance"] == "unknown"


class TestEditorialStanceReviews:
    def test_upsert_creates_review(self, repo: RunHistoryRepository) -> None:
        repo.upsert_editorial_stance_review(
            article_id="abc",
            original_stance="propaganda",
            corrected_stance="factual_report",
            comment="actually a factual deployment report",
        )
        r = repo.get_editorial_stance_review("abc")
        assert r is not None
        assert r["corrected_stance"] == "factual_report"
        assert r["original_stance"] == "propaganda"
        assert "factual" in str(r["comment"])

    def test_upsert_overwrites_existing(self, repo: RunHistoryRepository) -> None:
        repo.upsert_editorial_stance_review(
            article_id="abc",
            original_stance="propaganda",
            corrected_stance="factual_report",
        )
        repo.upsert_editorial_stance_review(
            article_id="abc",
            original_stance="propaganda",
            corrected_stance="analytical",
        )
        r = repo.get_editorial_stance_review("abc")
        assert r is not None
        assert r["corrected_stance"] == "analytical"

    def test_get_returns_none_when_no_review(self, repo: RunHistoryRepository) -> None:
        assert repo.get_editorial_stance_review("nonexistent") is None


class TestUpdateArticleEditorialStance:
    """訂正の articles 本体への還流 (2026-07-31 運用レビュー完全調査)。"""

    def test_updates_article_stance(self, repo: RunHistoryRepository) -> None:
        _add(repo, article_id="a1", title="t", feed_title="f", stance="propaganda")
        assert repo.update_article_editorial_stance("a1", "factual_report") == 1
        arts = repo.list_recent_articles_with_stance(stance_filter="factual_report")
        assert [a.article_id for a in arts] == ["a1"]

    def test_returns_zero_for_unknown_article(self, repo: RunHistoryRepository) -> None:
        assert repo.update_article_editorial_stance("nope", "analytical") == 0
