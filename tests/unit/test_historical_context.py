"""過去文脈 retrieval (articles_for_entity_keys) のテスト。

対象期間より前の同一 entity 記事を ACH 証拠に引く仕組み (時間窓 + entity 一致 + 新しい順)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "hist.db")


def _seed(
    repo: RunHistoryRepository,
    aid: str,
    *,
    when: datetime,
    entities: list[tuple[str, str]],
) -> None:
    run_id = repo.start_run(RunRecord(started_at=when, pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=aid,
            title=f"t-{aid}",
            url=f"http://x/{aid}",
            importance="high",
            status="posted",
        )
    )
    # created_at を when に上書き (時間窓テスト用)
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE articles SET created_at=? WHERE article_id=?",
            (when.isoformat(), aid),
        )
    repo.add_article_entities(aid, entities, when=when)


def test_retrieves_entity_matches_in_window_newest_first(repo: RunHistoryRepository) -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    # 過去 (30日前 / 60日前) に同一 actor、期間内 (1日前) にも同一 actor
    _seed(repo, "old1", when=now - timedelta(days=30), entities=[("actor", "salt_typhoon")])
    _seed(repo, "old2", when=now - timedelta(days=60), entities=[("actor", "salt_typhoon")])
    _seed(repo, "cur", when=now - timedelta(hours=2), entities=[("actor", "salt_typhoon")])
    _seed(repo, "other", when=now - timedelta(days=20), entities=[("actor", "lazarus")])

    # 対象期間 = 直近24h。過去窓 = [now-90d, now-24h)
    got = repo.articles_for_entity_keys(
        {"actor:salt_typhoon"},
        since=now - timedelta(days=90),
        before=now - timedelta(hours=24),
        limit=10,
    )
    assert got == ["old1", "old2"]  # 新しい順、期間内(cur)と別entity(other)は除外


def test_before_excludes_current_pool(repo: RunHistoryRepository) -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    _seed(repo, "cur", when=now - timedelta(hours=1), entities=[("cve", "CVE-2026-1")])
    got = repo.articles_for_entity_keys(
        {"cve:CVE-2026-1"}, since=now - timedelta(days=90), before=now - timedelta(hours=24)
    )
    assert got == []  # before(=期間開始)より新しい cur は過去文脈に含めない


def test_empty_anchors_returns_empty(repo: RunHistoryRepository) -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    assert repo.articles_for_entity_keys(set(), since=now - timedelta(days=90), before=now) == []
    assert repo.articles_for_entity_keys({"no_colon"}, since=now, before=now) == []
