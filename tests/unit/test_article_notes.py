"""Phase 5 (学習・記憶): article_notes storage CRUD のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.storage.run_history import ArticleNoteRecord, RunHistoryRepository


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "notes.db")


class TestArticleNotesCrud:
    def test_upsert_and_get(self, repo: RunHistoryRepository) -> None:
        repo.upsert_article_note(
            ArticleNoteRecord(
                article_id="a1",
                bookmarked=True,
                note="重要な先例",
                tags=["apt", "要追跡"],
                judgment="重要先例",
            )
        )
        n = repo.get_article_note("a1")
        assert n is not None
        assert n.bookmarked is True
        assert n.note == "重要な先例"
        assert n.tags == ["apt", "要追跡"]
        assert n.judgment == "重要先例"

    def test_get_missing_returns_none(self, repo: RunHistoryRepository) -> None:
        assert repo.get_article_note("nope") is None

    def test_upsert_preserves_created_at(self, repo: RunHistoryRepository) -> None:
        repo.upsert_article_note(ArticleNoteRecord(article_id="a1", note="v1"))
        first = repo.get_article_note("a1")
        assert first is not None
        repo.upsert_article_note(
            ArticleNoteRecord(article_id="a1", note="v2", created_at=first.created_at)
        )
        second = repo.get_article_note("a1")
        assert second is not None
        assert second.note == "v2"
        assert second.created_at == first.created_at  # created_at 保持

    def test_list_bookmarked_only(self, repo: RunHistoryRepository) -> None:
        repo.upsert_article_note(ArticleNoteRecord(article_id="b1", bookmarked=True))
        repo.upsert_article_note(ArticleNoteRecord(article_id="b2", bookmarked=False, note="x"))
        marked = repo.list_article_notes(bookmarked_only=True)
        assert [n.article_id for n in marked] == ["b1"]
        all_notes = repo.list_article_notes()
        assert {n.article_id for n in all_notes} == {"b1", "b2"}

    def test_list_filter_by_tag(self, repo: RunHistoryRepository) -> None:
        repo.upsert_article_note(ArticleNoteRecord(article_id="t1", tags=["china", "apt"]))
        repo.upsert_article_note(ArticleNoteRecord(article_id="t2", tags=["ransomware"]))
        china = repo.list_article_notes(tag="china")
        assert [n.article_id for n in china] == ["t1"]

    def test_delete(self, repo: RunHistoryRepository) -> None:
        repo.upsert_article_note(ArticleNoteRecord(article_id="d1", note="x"))
        assert repo.delete_article_note("d1") == 1
        assert repo.get_article_note("d1") is None

    def test_get_articles_by_ids(self, repo: RunHistoryRepository) -> None:
        from datetime import UTC, datetime

        from src.storage.run_history import ArticleRecord, RunRecord

        rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
        repo.add_article(
            ArticleRecord(run_id=rid, article_id="x1", title="記事X1", url="u", status="posted")
        )
        got = repo.get_articles_by_ids(["x1", "missing"])
        assert "x1" in got
        assert got["x1"].title == "記事X1"
        assert "missing" not in got
