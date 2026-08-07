"""subscription_analytics の Source Identity Decoupling (Stage 3/4) テスト。

統計が可変な feed_title でなく安定キー feed_url で結合されることを検証する。
これにより表示名 (feed_title) を改名しても統計が割れない (= 改名が構造的に安全)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.services.subscription_analytics import fetch_all_feed_stats


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "sa.db")


def _add(
    repo: RunHistoryRepository, run_id: int, aid: str, *, feed_title: str, feed_url: str
) -> None:
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=aid,
            title="t",
            url=f"https://e/{aid}",
            feed_title=feed_title,
            feed_url=feed_url,
            status="posted",
            importance="high",
            posted_channel="alert",
            created_at=datetime.now(UTC),
        )
    )


class TestFeedUrlGrouping:
    def test_rename_does_not_split_stats(self, repo: RunHistoryRepository) -> None:
        """改名前後で feed_title が違っても、同一 feed_url の記事は 1 source に集約される。"""
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        # 改名前の記事と改名後の記事 (feed_title 異なるが feed_url 同一)
        _add(repo, run_id, "a1", feed_title="Old Name", feed_url="https://e/feed")
        _add(repo, run_id, "a2", feed_title="New Name", feed_url="https://e/feed")

        stats = fetch_all_feed_stats(db_path=repo._db_path)
        # feed_url で 1 グループに集約 (改名で割れない)
        assert "https://e/feed" in stats
        assert stats["https://e/feed"].posted_count == 2
        # 旧来の feed_title キーでは引けない (= title でなく url が結合キー)
        assert "Old Name" not in stats
        assert "New Name" not in stats

    def test_legacy_null_feed_url_falls_back_to_title(self, repo: RunHistoryRepository) -> None:
        """feed_url 未充足 (旧記事) は feed_title に fallback して失われない。"""
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="leg1",
                title="t",
                url="https://e/leg1",
                feed_title="Legacy Feed",
                feed_url=None,
                status="posted",
                created_at=datetime.now(UTC),
            )
        )
        stats = fetch_all_feed_stats(db_path=repo._db_path)
        assert "Legacy Feed" in stats  # feed_url NULL → feed_title key に fallback
        assert stats["Legacy Feed"].posted_count == 1
