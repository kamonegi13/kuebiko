"""sitemap 由来記事の公開日と、初回流入ガードのテスト (2026-08-16)。

核心の不変量:
- ``published`` は sitemap の ``lastmod`` を使う (取込時刻で上書きしない)
- lastmod が無い場合のみ取込時刻へ fallback し、``published_is_placeholder`` を立てる
- 未見でも ``unseen_max_age_days`` より古い lastmod は取り込まない (新規登録時の流入防止)
- 古くて捨てた URL も seen 化する (毎回判定をやり直さない終端設計)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.watchers.sitemap_base import SitemapWatcher, parse_lastmod


def _watcher(tmp_path: Path, **kw: object) -> SitemapWatcher:
    return SitemapWatcher(
        name="t",
        sitemap_urls=("https://example.com/sitemap.xml",),
        url_include_pattern=re.compile(r"/news/"),
        state_file=tmp_path / "seen.json",
        feed_title="T",
        **kw,  # type: ignore[arg-type]
    )


class TestParseLastmod:
    def test_date_only_becomes_utc_aware(self) -> None:
        assert parse_lastmod("2026-07-30") == datetime(2026, 7, 30, tzinfo=UTC)

    def test_tz_aware_is_preserved(self) -> None:
        parsed = parse_lastmod("2026-07-30T12:00:00+09:00")
        assert parsed is not None and parsed.utcoffset() == timedelta(hours=9)

    def test_z_suffix_is_accepted(self) -> None:
        assert parse_lastmod("2026-07-30T00:00:00Z") == datetime(2026, 7, 30, tzinfo=UTC)

    @pytest.mark.parametrize("bad", ["", "   ", "not-a-date", None])
    def test_unparsable_is_none(self, bad: str | None) -> None:
        assert parse_lastmod(bad) is None


class TestPublishedComesFromLastmod:
    def test_lastmod_is_used_as_published(self, tmp_path: Path) -> None:
        w = _watcher(tmp_path)
        lastmod = datetime(2026, 8, 1, tzinfo=UTC)

        article = w._url_to_article("https://example.com/news/x", lastmod)

        assert article.published == lastmod
        assert article.published_is_placeholder is False

    def test_missing_lastmod_falls_back_and_flags(self, tmp_path: Path) -> None:
        w = _watcher(tmp_path)

        article = w._url_to_article("https://example.com/news/x", None)

        assert article.published_is_placeholder is True
        assert (datetime.now(UTC) - article.published) < timedelta(minutes=5)


class TestUnseenAgeGuard:
    @pytest.mark.asyncio
    async def test_old_entries_are_not_ingested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fresh = datetime.now(UTC) - timedelta(days=1)
        stale = datetime.now(UTC) - timedelta(days=400)
        entries = [
            ("https://example.com/news/new", fresh),
            ("https://example.com/news/old", stale),
        ]
        monkeypatch.setattr(SitemapWatcher, "_collect_all_entries", AsyncMock(return_value=entries))
        w = _watcher(tmp_path)

        articles = await w.fetch_articles()

        assert [a.url for a in articles] == ["https://example.com/news/new"]

    @pytest.mark.asyncio
    async def test_dropped_entries_are_still_marked_seen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """捨てた URL を seen 化しないと、毎回 lastmod 比較をやり直し続ける。"""
        stale = datetime.now(UTC) - timedelta(days=400)
        monkeypatch.setattr(
            SitemapWatcher,
            "_collect_all_entries",
            AsyncMock(return_value=[("https://example.com/news/old", stale)]),
        )
        w = _watcher(tmp_path)

        await w.fetch_articles()

        assert "https://example.com/news/old" in w._load_seen()

    @pytest.mark.asyncio
    async def test_missing_lastmod_is_not_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """古さを判定できない URL は通す (fail-open)。"""
        monkeypatch.setattr(
            SitemapWatcher,
            "_collect_all_entries",
            AsyncMock(return_value=[("https://example.com/news/x", None)]),
        )
        w = _watcher(tmp_path)

        articles = await w.fetch_articles()

        assert len(articles) == 1

    @pytest.mark.asyncio
    async def test_guard_can_be_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = datetime.now(UTC) - timedelta(days=400)
        monkeypatch.setattr(
            SitemapWatcher,
            "_collect_all_entries",
            AsyncMock(return_value=[("https://example.com/news/old", stale)]),
        )
        w = _watcher(tmp_path, unseen_max_age_days=0)

        articles = await w.fetch_articles()

        assert len(articles) == 1

    @pytest.mark.asyncio
    async def test_capped_overflow_is_not_marked_seen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cap 超過分を既読化すると次回以降取れず記事を永久に失う。"""
        fresh = datetime.now(UTC) - timedelta(days=1)
        entries = [(f"https://example.com/news/{i}", fresh) for i in range(5)]
        monkeypatch.setattr(SitemapWatcher, "_collect_all_entries", AsyncMock(return_value=entries))
        w = _watcher(tmp_path, max_posts_per_run=2)

        articles = await w.fetch_articles()
        seen = w._load_seen()

        assert len(articles) == 2
        assert len(seen) == 2, "返した 2 件だけが既読。残り 3 件は次回に回る"
