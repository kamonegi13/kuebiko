"""Phase 2 K1: セマンティック検索 storage 層のテスト。

``find_similar_embeddings`` (top-K cosine) / ``get_articles_by_urls`` /
``get_article`` を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "semantic.db")


def _seed_embedding(
    repo: RunHistoryRepository,
    *,
    url: str,
    h: str,
    vector: list[float],
    model: str = "m",
    when: datetime | None = None,
) -> None:
    repo.mark_url_seen(url_hash=h, url=url, title="t", when=when)
    repo.add_article_embedding(
        url_hash=h,
        url=url,
        vector=vector,
        model=model,
        when=when,
    )


def _add_article(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    url: str,
    title: str = "T",
    importance: str = "high",
    summary: str | None = "要約",
) -> int:
    run_id = repo.start_run(
        RunRecord(started_at=datetime.now(UTC), pipeline="daily", dry_run=False),
    )
    return repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title=title,
            url=url,
            importance=importance,
            category="apt",
            status="posted",
            summary=summary,
        ),
    )


class TestFindSimilarEmbeddings:
    def test_empty_returns_empty_list(self, repo: RunHistoryRepository) -> None:
        assert repo.find_similar_embeddings([0.1, 0.2], model="m") == []

    def test_orders_by_similarity_desc(self, repo: RunHistoryRepository) -> None:
        _seed_embedding(repo, url="https://e/p", h="p" * 32, vector=[1.0, 0.0])
        _seed_embedding(repo, url="https://e/q", h="q" * 32, vector=[0.7, 0.7])
        _seed_embedding(repo, url="https://e/r", h="r" * 32, vector=[0.0, 1.0])
        # クエリ [1, 0]: p が最も近く、次が q、r は直交。
        hits = repo.find_similar_embeddings([1.0, 0.0], model="m", threshold=0.0)
        assert [h[0] for h in hits] == ["p" * 32, "q" * 32, "r" * 32]
        # similarity 単調減少
        sims = [h[2] for h in hits]
        assert sims == sorted(sims, reverse=True)
        # url も返る
        assert hits[0][1] == "https://e/p"

    def test_threshold_filters(self, repo: RunHistoryRepository) -> None:
        _seed_embedding(repo, url="https://e/p", h="p" * 32, vector=[1.0, 0.0])
        _seed_embedding(repo, url="https://e/r", h="r" * 32, vector=[0.0, 1.0])
        hits = repo.find_similar_embeddings([1.0, 0.0], model="m", threshold=0.5)
        assert [h[0] for h in hits] == ["p" * 32]

    def test_top_k_limits(self, repo: RunHistoryRepository) -> None:
        for i in range(5):
            _seed_embedding(
                repo,
                url=f"https://e/{i}",
                h=f"h{i:031d}x",
                vector=[1.0, float(i) * 0.01],
            )
        hits = repo.find_similar_embeddings([1.0, 0.0], model="m", top_k=2, threshold=0.0)
        assert len(hits) == 2

    def test_filters_by_model(self, repo: RunHistoryRepository) -> None:
        _seed_embedding(repo, url="https://e/a", h="a" * 32, vector=[1.0, 0.0], model="m1")
        _seed_embedding(repo, url="https://e/b", h="b" * 32, vector=[1.0, 0.0], model="m2")
        hits = repo.find_similar_embeddings([1.0, 0.0], model="m1", threshold=0.0)
        assert [h[1] for h in hits] == ["https://e/a"]

    def test_window_hours_excludes_old(self, repo: RunHistoryRepository) -> None:
        old = datetime.now(UTC) - timedelta(hours=100)
        _seed_embedding(repo, url="https://e/old", h="o" * 32, vector=[1.0, 0.0], when=old)
        _seed_embedding(repo, url="https://e/new", h="n" * 32, vector=[1.0, 0.0])
        hits = repo.find_similar_embeddings([1.0, 0.0], model="m", threshold=0.0, window_hours=24)
        assert [h[1] for h in hits] == ["https://e/new"]

    def test_zero_query_returns_empty(self, repo: RunHistoryRepository) -> None:
        _seed_embedding(repo, url="https://e/p", h="p" * 32, vector=[1.0, 0.0])
        assert repo.find_similar_embeddings([0.0, 0.0], model="m") == []


class TestGetArticlesByUrls:
    def test_resolves_metadata(self, repo: RunHistoryRepository) -> None:
        _add_article(repo, article_id="tag:a", url="https://e/a", title="記事A")
        by_url = repo.get_articles_by_urls(["https://e/a"])
        assert "https://e/a" in by_url
        assert by_url["https://e/a"].title == "記事A"

    def test_missing_url_absent(self, repo: RunHistoryRepository) -> None:
        assert repo.get_articles_by_urls(["https://nope/x"]) == {}

    def test_empty_input(self, repo: RunHistoryRepository) -> None:
        assert repo.get_articles_by_urls([]) == {}

    def test_returns_latest_per_url(self, repo: RunHistoryRepository) -> None:
        # 同 URL に 2 行 (再投稿)。created_at 最新の title が返る。
        _add_article(repo, article_id="tag:old", url="https://e/dup", title="旧")
        _add_article(repo, article_id="tag:new", url="https://e/dup", title="新")
        by_url = repo.get_articles_by_urls(["https://e/dup"])
        assert by_url["https://e/dup"].title == "新"


class TestGetArticle:
    def test_returns_record(self, repo: RunHistoryRepository) -> None:
        _add_article(repo, article_id="tag:x", url="https://e/x", summary="要約X")
        rec = repo.get_article("tag:x")
        assert rec is not None
        assert rec.url == "https://e/x"
        assert rec.summary == "要約X"

    def test_missing_returns_none(self, repo: RunHistoryRepository) -> None:
        assert repo.get_article("tag:nope") is None
