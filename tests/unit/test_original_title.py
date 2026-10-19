"""articles.original_title の保存と、dedup_seen_urls からの埋め直し。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.original_title_backfill import apply_backfill, count_backfillable
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "test.db")


def _run(repo: RunHistoryRepository) -> int:
    return repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="daily", dry_run=False))


def _add(
    repo: RunHistoryRepository, run_id: int, article_id: str, original_title: str | None = None
) -> None:
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title="日本語の見出し",
            url="u",
            status="posted",
            original_title=original_title,
        )
    )


def _seen(repo: RunHistoryRepository, article_id: str, title: str | None) -> None:
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO dedup_seen_urls (url_hash, url, article_id, title, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"h-{article_id}",
                f"https://x.example/{article_id}",
                article_id,
                title,
                "2026-07-01",
                "2026-07-01",
            ),
        )


def _original(repo: RunHistoryRepository, article_id: str) -> str | None:
    with repo._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT original_title FROM articles WHERE article_id=?", (article_id,)
        ).fetchone()
    return None if row[0] is None else str(row[0])


def test_original_title_is_stored_and_read_back(repo: RunHistoryRepository) -> None:
    run_id = _run(repo)
    _add(repo, run_id, "a1", original_title="Original English Title")
    assert _original(repo, "a1") == "Original English Title"
    rec = repo.get_article("a1")
    assert rec is not None and rec.original_title == "Original English Title"


def test_original_title_defaults_to_none(repo: RunHistoryRepository) -> None:
    run_id = _run(repo)
    _add(repo, run_id, "a1")
    assert _original(repo, "a1") is None


def test_backfill_fills_only_null_rows_that_dedup_knows(repo: RunHistoryRepository) -> None:
    run_id = _run(repo)
    _add(repo, run_id, "has_dedup")
    _add(repo, run_id, "no_dedup")
    _add(repo, run_id, "already", original_title="keep me")
    _add(repo, run_id, "empty_dedup")
    _seen(repo, "has_dedup", "Dedup English Title")
    _seen(repo, "already", "Other")
    _seen(repo, "empty_dedup", "")
    with repo._connect() as conn:  # noqa: SLF001
        assert count_backfillable(conn) == 1
        assert apply_backfill(conn) == 1
    assert _original(repo, "has_dedup") == "Dedup English Title"
    assert _original(repo, "no_dedup") is None
    assert _original(repo, "already") == "keep me"
    assert _original(repo, "empty_dedup") is None


def test_backfill_is_idempotent(repo: RunHistoryRepository) -> None:
    run_id = _run(repo)
    _add(repo, run_id, "a1")
    _seen(repo, "a1", "T")
    with repo._connect() as conn:  # noqa: SLF001
        assert apply_backfill(conn) == 1
    with repo._connect() as conn:  # noqa: SLF001
        assert count_backfillable(conn) == 0
        assert apply_backfill(conn) == 0
