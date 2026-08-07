"""articles.body_ja (本文オンデマンド日本語訳キャッシュ) の repo 層テスト。

get/update の往復と、90 日 retention purge が body と body_ja を同時に消すことを検証。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "body_ja.db")


def _add(repo: RunHistoryRepository, aid: str, body: str | None = None) -> None:
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid, article_id=aid, title=f"t-{aid}", url=f"u-{aid}", status="posted"
        ),
    )
    if body is not None:
        repo.update_article_body(aid, body)


def test_body_ja_roundtrip(repo: RunHistoryRepository) -> None:
    _add(repo, "a1", body="original english body")
    assert repo.get_article_body_ja("a1") is None

    assert repo.update_article_body_ja("a1", "日本語訳の本文") == 1
    assert repo.get_article_body_ja("a1") == "日本語訳の本文"
    # 原文は保持されたまま
    assert repo.get_article_body("a1") == "original english body"


def test_body_ja_overwrite(repo: RunHistoryRepository) -> None:
    _add(repo, "a1", body="body")
    repo.update_article_body_ja("a1", "旧訳")
    repo.update_article_body_ja("a1", "新訳")
    assert repo.get_article_body_ja("a1") == "新訳"


def test_body_ja_unknown_article(repo: RunHistoryRepository) -> None:
    assert repo.get_article_body_ja("missing") is None
    assert repo.update_article_body_ja("missing", "訳") == 0


def test_purge_removes_body_and_body_ja_together(repo: RunHistoryRepository) -> None:
    # 古い記事 (100 日前取得) と新しい記事を用意し、両方に訳キャッシュを付ける
    _add(repo, "old")
    repo.update_article_body("old", "old body", fetched_at=datetime.now(UTC) - timedelta(days=100))
    repo.update_article_body_ja("old", "古い訳")
    _add(repo, "new", body="new body")
    repo.update_article_body_ja("new", "新しい訳")

    purged = repo.purge_article_bodies_older_than(days=90)

    assert purged == 1
    # 原文なしで訳だけ残る不整合を作らない (body と body_ja は同時に NULL 化)
    assert repo.get_article_body("old") is None
    assert repo.get_article_body_ja("old") is None
    assert repo.get_article_body("new") == "new body"
    assert repo.get_article_body_ja("new") == "新しい訳"
