"""Unit tests for src.tools.direct_rss_source."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.tools.article_model import Article
from src.tools.direct_rss_source import (
    DirectRssSource,
    FeedDef,
    FetchResult,
    SeenHashFilter,
    _entry_to_article,
    load_feeds_config,
)


class TestFeedsConfigLoading:
    def test_loads_yaml_file(self, tmp_path: Path) -> None:
        yaml_text = """
feeds:
  - name: "Test Feed 1"
    url: "https://example.com/feed1.xml"
    enabled: true
  - name: "Test Feed 2"
    url: "https://example.com/feed2.xml"
    enabled: false
    folder: "research"
"""
        p = tmp_path / "feeds.yaml"
        p.write_text(yaml_text)
        cfg = load_feeds_config(p)
        assert len(cfg.feeds) == 2
        assert cfg.feeds[0].name == "Test Feed 1"
        assert cfg.feeds[0].enabled is True
        assert cfg.feeds[1].enabled is False
        assert cfg.feeds[1].folder == "research"

    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        cfg = load_feeds_config(tmp_path / "missing.yaml")
        assert cfg.feeds == []

    def test_returns_empty_when_yaml_has_no_feeds(self, tmp_path: Path) -> None:
        p = tmp_path / "feeds.yaml"
        p.write_text("feeds: []")
        cfg = load_feeds_config(p)
        assert cfg.feeds == []


class TestEntryToArticle:
    def test_basic_entry_with_all_fields(self) -> None:
        from time import struct_time

        entry = {
            "id": "https://example.com/post-1",
            "link": "https://example.com/post-1",
            "title": "Test Article Title",
            "summary": "Test summary HTML",
            "author": "John Doe",
            "published_parsed": struct_time((2026, 5, 25, 10, 30, 0, 0, 145, 0)),
        }
        feed = FeedDef(name="Test Feed", url="https://example.com/feed.xml")
        article = _entry_to_article(entry, feed)
        assert article is not None
        assert article.id == "rss:https://example.com/post-1"
        assert article.title == "Test Article Title"
        assert article.url == "https://example.com/post-1"
        assert article.author == "John Doe"
        assert article.feed_title == "Test Feed"
        assert article.published.year == 2026

    def test_skips_entry_without_link(self) -> None:
        entry = {
            "title": "No Link",
            "summary": "x",
        }
        feed = FeedDef(name="Test", url="https://example.com/feed.xml")
        assert _entry_to_article(entry, feed) is None

    def test_uses_url_hash_when_id_missing(self) -> None:
        entry = {
            "link": "https://example.com/post-2",
            "title": "Title",
        }
        feed = FeedDef(name="Test", url="https://example.com/feed.xml")
        article = _entry_to_article(entry, feed)
        assert article is not None
        assert article.id.startswith("rss:")
        # hash is 16 chars
        assert len(article.id) == len("rss:") + 16

    def test_falls_back_to_updated_when_published_missing(self) -> None:
        from time import struct_time

        entry = {
            "link": "https://example.com/post-3",
            "title": "T",
            "updated_parsed": struct_time((2026, 1, 1, 0, 0, 0, 0, 1, 0)),
        }
        feed = FeedDef(name="T", url="https://x.com/feed.xml")
        article = _entry_to_article(entry, feed)
        assert article is not None
        assert article.published.year == 2026
        assert article.published.month == 1

    def test_handles_empty_title(self) -> None:
        entry = {"link": "https://x.com/a", "title": ""}
        feed = FeedDef(name="T", url="https://x.com/feed.xml")
        article = _entry_to_article(entry, feed)
        assert article is not None
        assert article.title == "(no title)"

    def test_extracts_content_when_summary_missing(self) -> None:
        entry = {
            "link": "https://x.com/a",
            "title": "T",
            "content": [{"value": "<p>content body</p>", "type": "text/html"}],
        }
        feed = FeedDef(name="T", url="https://x.com/feed.xml")
        article = _entry_to_article(entry, feed)
        assert article is not None
        assert "content body" in article.summary_html


class TestDirectRssSourceFetch:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_feeds(self) -> None:
        source = DirectRssSource(feeds=[])
        result = await source.fetch(max_count=10)
        assert result == []

    @pytest.mark.asyncio
    async def test_filters_disabled_feeds(self) -> None:
        feeds = [
            FeedDef(name="A", url="https://a.example/feed.xml", enabled=True),
            FeedDef(name="B", url="https://b.example/feed.xml", enabled=False),
        ]
        source = DirectRssSource(feeds=feeds)
        assert len(source._feeds) == 1  # noqa: SLF001
        assert source._feeds[0].name == "A"  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_fetch_handles_http_error_gracefully(self) -> None:
        from src.tools.direct_rss_source import FetchResult

        feeds = [FeedDef(name="A", url="https://a.example/feed.xml")]
        source = DirectRssSource(feeds=feeds)

        mock_result = FetchResult(feed=feeds[0], articles=[], error="http: timeout")
        with patch(
            "src.tools.direct_rss_source._fetch_one_feed",
            new=AsyncMock(return_value=mock_result),
        ):
            result = await source.fetch(max_count=10)
        # 失敗 feed は drop、空 list が返る
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_sorts_articles_by_published_desc(self) -> None:
        from src.tools.article_model import Article
        from src.tools.direct_rss_source import FetchResult

        old_article = Article(
            id="rss:old",
            title="Old",
            url="https://a.example/old",
            summary_html="",
            published=datetime(2026, 5, 1, tzinfo=UTC),
            feed_title="A",
            feed_url="https://a.example/feed.xml",
        )
        new_article = Article(
            id="rss:new",
            title="New",
            url="https://a.example/new",
            summary_html="",
            published=datetime(2026, 5, 25, tzinfo=UTC),
            feed_title="A",
            feed_url="https://a.example/feed.xml",
        )

        feeds = [FeedDef(name="A", url="https://a.example/feed.xml")]
        source = DirectRssSource(feeds=feeds)
        mock_result = FetchResult(feed=feeds[0], articles=[old_article, new_article])

        with patch(
            "src.tools.direct_rss_source._fetch_one_feed",
            new=AsyncMock(return_value=mock_result),
        ):
            result = await source.fetch(max_count=10)

        assert len(result) == 2
        assert result[0].id == "rss:new"
        assert result[1].id == "rss:old"

    @pytest.mark.asyncio
    async def test_fetch_respects_max_count(self) -> None:
        from src.tools.article_model import Article
        from src.tools.direct_rss_source import FetchResult

        articles = [
            Article(
                id=f"rss:{i}",
                title=f"Title {i}",
                url=f"https://a.example/{i}",
                summary_html="",
                published=datetime(2026, 5, 25 - i, tzinfo=UTC),
                feed_title="A",
                feed_url="https://a.example/feed.xml",
            )
            for i in range(10)
        ]
        feeds = [FeedDef(name="A", url="https://a.example/feed.xml")]
        source = DirectRssSource(feeds=feeds)
        mock_result = FetchResult(feed=feeds[0], articles=articles)

        with patch(
            "src.tools.direct_rss_source._fetch_one_feed",
            new=AsyncMock(return_value=mock_result),
        ):
            result = await source.fetch(max_count=3)
        assert len(result) == 3


def _art(feed: str, slug: str, days_ago: float) -> Article:
    """published が days_ago 日前の Article を作る (P0 テスト用 helper)。"""
    from datetime import timedelta

    return Article(
        id=f"rss:{feed}:{slug}",
        title=f"{feed} {slug}",
        url=f"https://{feed}.example/{slug}",
        summary_html="",
        published=datetime.now(UTC) - timedelta(days=days_ago),
        feed_title=feed,
        feed_url=f"https://{feed}.example/feed.xml",
    )


def _patch_feeds(
    results_by_feed: dict[str, list[Article]],
) -> AbstractContextManager[Any]:
    """_fetch_one_feed を feed 名で振り分ける patch を返す。"""

    async def _fake(
        client: httpx.AsyncClient,
        feed: FeedDef,
        per_feed_limit: int = 50,
        user_agent: str = "",
    ) -> FetchResult:
        return FetchResult(feed=feed, articles=results_by_feed.get(feed.name, []))

    return patch("src.tools.direct_rss_source._fetch_one_feed", new=_fake)


class TestUnseenFirstSelection:
    """P0 収集飢餓の根治: seen 除外を切り詰めの前に行う。

    旧実装は全 feed 混合 pool を published 降順 top-N に切ってから dedup していたため、
    低頻度/バッチ日付 feed の未見記事が高頻度 feed に永遠に押し出されていた
    (CISA KEV advisories 6 週間無音の実測病理)。
    """

    def _seen_filter_for(self, seen_urls: set[str]) -> SeenHashFilter:
        from src.tools.url_normalizer import url_hash

        seen_hashes = {url_hash(u) for u in seen_urls}

        def _filter(hashes: Sequence[str]) -> set[str]:
            return {h for h in hashes if h in seen_hashes}

        return _filter

    @pytest.mark.asyncio
    async def test_unseen_older_items_survive_seen_newer_flood(self) -> None:
        # feed A: 新しいが全て seen 済み 150 件 / feed B: 数日前だが未見 3 件
        noisy = [_art("noisy", f"n{i}", days_ago=0.01 * i) for i in range(150)]
        quiet = [_art("quiet", f"q{i}", days_ago=3 + i) for i in range(3)]
        feeds = [
            FeedDef(name="noisy", url="https://noisy.example/feed.xml"),
            FeedDef(name="quiet", url="https://quiet.example/feed.xml"),
        ]
        source = DirectRssSource(
            feeds=feeds,
            seen_hash_filter=self._seen_filter_for({a.url for a in noisy}),
        )
        with _patch_feeds({"noisy": noisy, "quiet": quiet}):
            result = await source.fetch(max_count=100)
        assert {a.feed_title for a in result} == {"quiet"}
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_fairness_across_feeds_when_unseen_exceeds_cap(self) -> None:
        # A に未見 20 (新しい)、B に未見 3 (古い)、max=10 → B の 3 件が必ず含まれる
        a = [_art("a", f"a{i}", days_ago=0.01 * i) for i in range(20)]
        b = [_art("b", f"b{i}", days_ago=5 + i) for i in range(3)]
        feeds = [
            FeedDef(name="a", url="https://a.example/feed.xml"),
            FeedDef(name="b", url="https://b.example/feed.xml"),
        ]
        source = DirectRssSource(feeds=feeds, seen_hash_filter=lambda h: set())
        with _patch_feeds({"a": a, "b": b}):
            result = await source.fetch(max_count=10)
        assert len(result) == 10
        assert sum(1 for x in result if x.feed_title == "b") == 3

    @pytest.mark.asyncio
    async def test_age_guard_excludes_stale_unseen(self) -> None:
        # 未見でも published が上限日数より古い item は返さない (新規 feed の過去記事流入防止)
        fresh = _art("f", "fresh", days_ago=2)
        stale = _art("f", "stale", days_ago=30)
        feeds = [FeedDef(name="f", url="https://f.example/feed.xml")]
        source = DirectRssSource(feeds=feeds, seen_hash_filter=lambda h: set())
        with _patch_feeds({"f": [fresh, stale]}):
            result = await source.fetch(max_count=10)
        assert [a.id for a in result] == [fresh.id]

    @pytest.mark.asyncio
    async def test_intra_batch_url_duplicate_removed(self) -> None:
        # 同一 URL が複数 feed から来たら 1 件に
        x1 = _art("f1", "same", days_ago=1)
        x2 = _art("f2", "other", days_ago=1).model_copy(update={"url": x1.url})
        feeds = [
            FeedDef(name="f1", url="https://f1.example/feed.xml"),
            FeedDef(name="f2", url="https://f2.example/feed.xml"),
        ]
        source = DirectRssSource(feeds=feeds, seen_hash_filter=lambda h: set())
        with _patch_feeds({"f1": [x1], "f2": [x2]}):
            result = await source.fetch(max_count=10)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_no_filter_keeps_legacy_topn_behavior(self) -> None:
        # seen_hash_filter 未指定なら従来どおり published 降順 top-N (独立利用/旧テスト互換)
        arts = [_art("a", f"x{i}", days_ago=i) for i in range(5)]
        feeds = [FeedDef(name="a", url="https://a.example/feed.xml")]
        source = DirectRssSource(feeds=feeds)
        with _patch_feeds({"a": arts}):
            result = await source.fetch(max_count=2)
        assert [a.id for a in result] == [arts[0].id, arts[1].id]


class TestFetchOneFeedEscalation:
    """本番 feed 取得の段階エスカレーション (2026-08-01 Security Joes 型の回帰防止)。

    プレビュー経路にしか無かった browser UA fallback を fetch_policy に集約し、
    本番の毎時 fetch も同じポリシーを通ることを固定する。
    """

    RSS_BODY = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        "<item><title>a1</title><link>https://x.test/a1</link>"
        "<pubDate>Thu, 30 Jul 2026 14:00:00 +0000</pubDate></item>"
        "</channel></rss>"
    )

    def _transport(self, bot_status: int, ua_log: list[str]) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            ua = request.headers.get("User-Agent", "")
            ua_log.append(ua)
            if ua.startswith("kuebiko/"):
                if bot_status != 200:
                    return httpx.Response(bot_status, text="blocked")
                return httpx.Response(200, text=self.RSS_BODY)
            return httpx.Response(200, text=self.RSS_BODY)

        return httpx.MockTransport(handler)

    @pytest.mark.asyncio
    async def test_bot_blocked_feed_recovers_via_browser_ua(self) -> None:
        from src.tools.direct_rss_source import _fetch_one_feed

        ua_log: list[str] = []
        feed = FeedDef(name="Blocked Feed", url="https://x.test/feed.xml")
        async with httpx.AsyncClient(transport=self._transport(403, ua_log)) as client:
            result = await _fetch_one_feed(client, feed, user_agent="kuebiko/1.0 (+test)")
        assert result.error is None
        assert len(result.articles) == 1
        assert result.stage == "browser"
        assert len(ua_log) == 2 and not ua_log[1].startswith("kuebiko/")

    @pytest.mark.asyncio
    async def test_healthy_feed_stays_on_bot_ua(self) -> None:
        from src.tools.direct_rss_source import _fetch_one_feed

        ua_log: list[str] = []
        feed = FeedDef(name="Healthy Feed", url="https://x.test/feed.xml")
        async with httpx.AsyncClient(transport=self._transport(200, ua_log)) as client:
            result = await _fetch_one_feed(client, feed, user_agent="kuebiko/1.0 (+test)")
        assert result.error is None
        assert result.stage == "bot"
        assert ua_log == ["kuebiko/1.0 (+test)"]  # 礼儀正しい既定のまま 1 回のみ

    @pytest.mark.asyncio
    async def test_non_block_error_is_reported_without_escalation(self) -> None:
        from src.tools.direct_rss_source import _fetch_one_feed

        ua_log: list[str] = []
        feed = FeedDef(name="Gone Feed", url="https://x.test/feed.xml")
        async with httpx.AsyncClient(transport=self._transport(404, ua_log)) as client:
            result = await _fetch_one_feed(client, feed, user_agent="kuebiko/1.0 (+test)")
        assert result.error is not None and "404" in result.error
        assert len(ua_log) == 1  # 404 は UA の問題ではないのでエスカレーションしない
