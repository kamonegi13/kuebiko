"""_persist_semantic_dedup_skips (src/pipeline/orchestrator.py) の unit test。

意味 dedup で落ちた記事の既読化 + 判断根拠 (embedding) 保存 + skip 記録
(dedup_semantic_skips、docs/event_news_design.md §8b) をまとめて検証する。
**dry-run では一切書かないこと** / **書込例外で pipeline を殺さないこと (fail-open)**
が核心の不変条件 (_mark_skipped_urls_seen と同じ検証形式、test_dedup_skip_mark_seen.py)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from src.pipeline.filters import SemanticSkip
from src.pipeline.orchestrator import _persist_semantic_dedup_skips
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article


class _FakeDedupRepo:
    """mark_url_seen / add_article_embedding / record_semantic_skips の呼出を記録する stub。"""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.embeddings: list[dict[str, Any]] = []
        self.skip_batches: list[list[SemanticSkip]] = []
        self.raise_on_record_skips = False

    def mark_url_seen(
        self,
        *,
        url_hash: str,
        url: str,
        article_id: str | None = None,
        title: str | None = None,
        when: datetime | None = None,
    ) -> None:
        self.seen.append({"url_hash": url_hash, "url": url, "article_id": article_id})

    def add_article_embedding(
        self,
        *,
        url_hash: str,
        url: str,
        vector: list[float],
        model: str,
        title: str | None = None,
    ) -> None:
        self.embeddings.append({"url_hash": url_hash, "url": url, "model": model})

    def record_semantic_skips(self, rows: list[SemanticSkip]) -> int:
        if self.raise_on_record_skips:
            raise RuntimeError("db down")
        self.skip_batches.append(list(rows))
        return len(rows)


def _repo(fake: _FakeDedupRepo) -> RunHistoryRepository:
    return cast(RunHistoryRepository, fake)


def _article(aid: str, url: str) -> Article:
    return Article(
        id=aid,
        title="t",
        url=url,
        summary_html="",
        published=datetime(2026, 8, 23, tzinfo=UTC),
        feed_title="f",
        feed_url="https://f.example/rss",
    )


def _skip(url: str = "https://x.example/1") -> SemanticSkip:
    return SemanticSkip(
        skipped_url=url,
        skipped_title="t",
        skipped_host="x.example",
        feed_title="f",
        feed_url="https://f.example/rss",
        tier="hard",
        matched_kind="url_hash",
        matched_key="abc123",
    )


def test_dry_run_writes_nothing() -> None:
    fake = _FakeDedupRepo()
    a = _article("a1", "https://x.example/1")
    _persist_semantic_dedup_skips(
        dry_run=True,
        skipped_semantic_ids=["a1"],
        pre_semantic_by_id={"a1": a},
        semantic_embeddings={},
        semantic_skip_records=[_skip()],
        dedup_repo=_repo(fake),
    )
    assert fake.seen == []
    assert fake.embeddings == []
    assert fake.skip_batches == []


def test_empty_skipped_ids_writes_nothing() -> None:
    fake = _FakeDedupRepo()
    _persist_semantic_dedup_skips(
        dry_run=False,
        skipped_semantic_ids=[],
        pre_semantic_by_id={},
        semantic_embeddings={},
        semantic_skip_records=[],
        dedup_repo=_repo(fake),
    )
    assert fake.seen == []
    assert fake.skip_batches == []


def test_marks_seen_and_records_skips() -> None:
    fake = _FakeDedupRepo()
    a = _article("a1", "https://x.example/1")
    rec = _skip()
    _persist_semantic_dedup_skips(
        dry_run=False,
        skipped_semantic_ids=["a1"],
        pre_semantic_by_id={"a1": a},
        semantic_embeddings={"a1": ("m", [1.0, 0.0])},
        semantic_skip_records=[rec],
        dedup_repo=_repo(fake),
    )
    assert len(fake.seen) == 1
    assert fake.seen[0]["article_id"] == "a1"
    assert len(fake.embeddings) == 1
    assert fake.skip_batches == [[rec]]


def test_record_semantic_skips_failure_is_swallowed() -> None:
    """skip 記録の書込失敗で pipeline 全体 (既読化含む) を壊さない (fail-open, §8b)。"""
    fake = _FakeDedupRepo()
    fake.raise_on_record_skips = True
    a = _article("a1", "https://x.example/1")
    # 例外を投げても呼び出しは正常終了する (raise しない)
    _persist_semantic_dedup_skips(
        dry_run=False,
        skipped_semantic_ids=["a1"],
        pre_semantic_by_id={"a1": a},
        semantic_embeddings={},
        semantic_skip_records=[_skip()],
        dedup_repo=_repo(fake),
    )
    # 既読化は skip 記録の失敗と独立して先に完了している
    assert len(fake.seen) == 1


def test_missing_article_in_map_is_skipped() -> None:
    fake = _FakeDedupRepo()
    _persist_semantic_dedup_skips(
        dry_run=False,
        skipped_semantic_ids=["absent"],
        pre_semantic_by_id={},
        semantic_embeddings={},
        semantic_skip_records=[],
        dedup_repo=_repo(fake),
    )
    assert fake.seen == []
