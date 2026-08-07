"""_mark_skipped_urls_seen (src/pipeline/orchestrator.py) の unit test。

dedup skip 経路 (cross-ch / CVE / content) の URL を dedup_seen_urls に既読化し、
次 run の再 fetch→再評価リサイクル (取込履歴の毎時フラッド + 48h/24h 窓後の同一 URL
再投稿) を止める挙動を検証する。2026-07-30 の根治。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from src.pipeline.orchestrator import _mark_skipped_urls_seen
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.url_normalizer import url_hash


class _FakeDedupRepo:
    """mark_url_seen の呼出を記録するだけの stub。"""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    def mark_url_seen(
        self,
        *,
        url_hash: str,
        url: str,
        article_id: str | None = None,
        title: str | None = None,
        when: datetime | None = None,
    ) -> None:
        self.seen.append(
            {"url_hash": url_hash, "url": url, "article_id": article_id, "title": title}
        )


def _repo(fake: _FakeDedupRepo) -> RunHistoryRepository:
    return cast(RunHistoryRepository, fake)


def _article(aid: str, url: str, title: str = "t") -> Article:
    return Article(
        id=aid,
        title=title,
        url=url,
        summary_html="",
        published=datetime(2026, 7, 30, tzinfo=UTC),
        feed_title="f",
        feed_url="https://f.example/rss",
    )


def test_marks_skipped_url_seen() -> None:
    fake = _FakeDedupRepo()
    a = _article("a1", "https://ex.com/1", "VMware vuln")
    marked = _mark_skipped_urls_seen(["a1"], {"a1": a}, _repo(fake))
    assert marked == 1
    assert len(fake.seen) == 1
    assert fake.seen[0]["url"] == "https://ex.com/1"
    assert fake.seen[0]["url_hash"] == url_hash("https://ex.com/1")
    assert fake.seen[0]["title"] == "VMware vuln"
    assert fake.seen[0]["article_id"] == "a1"


def test_absent_ids_are_skipped() -> None:
    # semantic/triage/URL-dedup 早期 skip は articles_by_id に無い → 素通り (既読化済のため)
    fake = _FakeDedupRepo()
    a = _article("a1", "https://ex.com/1")
    marked = _mark_skipped_urls_seen(["a1", "absent"], {"a1": a}, _repo(fake))
    assert marked == 1
    assert [s["article_id"] for s in fake.seen] == ["a1"]


def test_duplicate_ids_marked_once() -> None:
    fake = _FakeDedupRepo()
    a = _article("a1", "https://ex.com/1")
    marked = _mark_skipped_urls_seen(["a1", "a1", "a1"], {"a1": a}, _repo(fake))
    assert marked == 1
    assert len(fake.seen) == 1


def test_mark_failure_is_swallowed() -> None:
    class _RaisingRepo(_FakeDedupRepo):
        def mark_url_seen(self, **kwargs: Any) -> None:
            raise RuntimeError("db down")

    fake = _RaisingRepo()
    a = _article("a1", "https://ex.com/1")
    # 例外を握りつぶし、次 run 再評価で自癒 (run 全体は壊さない)
    marked = _mark_skipped_urls_seen(["a1"], {"a1": a}, _repo(fake))
    assert marked == 0


def test_empty_input() -> None:
    fake = _FakeDedupRepo()
    assert _mark_skipped_urls_seen([], {}, _repo(fake)) == 0
    assert fake.seen == []
