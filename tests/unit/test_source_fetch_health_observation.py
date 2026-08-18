"""transport 横断の死活観測テスト (2026-08-02)。

不変量: **「新着 0 件」と「取得が壊れた」は必ず区別できること**。
成果 (articles) ではなく行為 (取得 + 抽出の成立) を記録することでのみ達成される。
html_scraper / sitemap は自前 selector / pattern の適用まで含めて「行為の成立」
(サイト改修で腐ると listing は 200 のまま抽出 0 件 = 無音で死ぬため)。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.tools.source_fetch_outcome import FetchObservation, SourceFetchOutcome
from src.watchers.html_listing import HtmlListingWatcher
from src.watchers.sitemap_base import SitemapWatcher

LISTING_URL = "https://example.com/blog"
SITEMAP_URL = "https://example.com/sitemap.xml"


def _listing_watcher(tmp_path: Path) -> HtmlListingWatcher:
    return HtmlListingWatcher(
        name="example-blog",
        listing_url=LISTING_URL,
        article_link_selector="a.post",
        state_file=tmp_path / "example_seen.json",
        feed_title="Example Blog",
    )


def _sitemap_watcher(tmp_path: Path) -> SitemapWatcher:
    return SitemapWatcher(
        name="example-sitemap",
        sitemap_urls=(SITEMAP_URL,),
        url_include_pattern=re.compile(r"/news/"),
        state_file=tmp_path / "example_sitemap_seen.json",
        feed_title="Example Sitemap",
    )


class TestFetchObservation:
    def test_unattempted_yields_no_outcome(self) -> None:
        # 走らなかった watcher の記録で既存の死活を上書きしない
        assert FetchObservation().to_outcome("k", "n") is None

    def test_success_and_failure_round_trip(self) -> None:
        obs = FetchObservation()
        obs.record_success(7)
        assert obs.to_outcome("k", "n") == SourceFetchOutcome("k", "n", True, 7, "")
        obs.record_failure("selector 不一致")
        out = obs.to_outcome("k", "n")
        assert out is not None
        assert (out.ok, out.item_count, out.error) == (False, 0, "selector 不一致")

    def test_as_row_matches_upsert_shape(self) -> None:
        row = SourceFetchOutcome("https://x/y", "N", True, 3, "").as_row()
        assert row == ("https://x/y", "N", True, "", 3)


class TestHtmlListingObservation:
    @pytest.mark.asyncio
    async def test_quiet_source_records_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """新着 0 件でも「取得できた」ことが残る (これが核心)。"""
        w = _listing_watcher(tmp_path)
        html = '<a class="post" href="/a">A</a><a class="post" href="/b">B</a>'
        monkeypatch.setattr(HtmlListingWatcher, "_fetch_listing", AsyncMock(return_value=html))
        w._save_seen({f"{LISTING_URL}/a", "https://example.com/a", "https://example.com/b"})

        articles = await w.fetch_articles()
        outcome = w.last_fetch_outcome()

        assert articles == []  # 新着なし
        assert outcome is not None
        assert outcome.ok is True  # だが取得は成立している
        assert outcome.item_count == 2
        assert outcome.source_key == LISTING_URL  # 購読一覧の url と一致 (結合キー)

    @pytest.mark.asyncio
    async def test_broken_selector_records_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """listing は 200 でも抽出 0 件 = サイト改修による無音死 → 失敗として残す。"""
        w = _listing_watcher(tmp_path)
        monkeypatch.setattr(
            HtmlListingWatcher, "_fetch_listing", AsyncMock(return_value="<div>redesigned</div>")
        )

        articles = await w.fetch_articles()
        outcome = w.last_fetch_outcome()

        assert articles == []
        assert outcome is not None
        assert outcome.ok is False
        assert "セレクタ" in outcome.error

    @pytest.mark.asyncio
    async def test_listing_fetch_failure_records_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w = _listing_watcher(tmp_path)
        monkeypatch.setattr(HtmlListingWatcher, "_fetch_listing", AsyncMock(return_value=""))

        await w.fetch_articles()
        outcome = w.last_fetch_outcome()

        assert outcome is not None and outcome.ok is False
        assert "listing" in outcome.error

    @pytest.mark.asyncio
    async def test_url_filter_mismatch_records_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w = HtmlListingWatcher(
            name="example-blog",
            listing_url=LISTING_URL,
            article_link_selector="a.post",
            state_file=tmp_path / "s.json",
            feed_title="Example Blog",
            url_include_pattern=r"/never-matches/",
        )
        monkeypatch.setattr(
            HtmlListingWatcher,
            "_fetch_listing",
            AsyncMock(return_value='<a class="post" href="/a">A</a>'),
        )

        await w.fetch_articles()
        outcome = w.last_fetch_outcome()

        assert outcome is not None and outcome.ok is False
        assert "フィルタ" in outcome.error


class TestSitemapObservation:
    @pytest.mark.asyncio
    async def test_quiet_sitemap_records_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w = _sitemap_watcher(tmp_path)
        urls = ["https://example.com/news/1", "https://example.com/news/2"]
        entries = [(u, None) for u in urls]
        monkeypatch.setattr(SitemapWatcher, "_collect_all_entries", AsyncMock(return_value=entries))
        w._save_seen(set(urls))

        articles = await w.fetch_articles()
        outcome = w.last_fetch_outcome()

        assert articles == []
        assert outcome is not None
        assert (outcome.ok, outcome.item_count) == (True, 2)
        assert outcome.source_key == SITEMAP_URL

    @pytest.mark.asyncio
    async def test_pattern_rot_records_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sitemap は取れるが include パターンに 1 件も一致しない = 構造変更。"""
        w = _sitemap_watcher(tmp_path)
        monkeypatch.setattr(
            SitemapWatcher,
            "_collect_all_entries",
            AsyncMock(return_value=[("https://example.com/articles/1", None)]),
        )

        await w.fetch_articles()
        outcome = w.last_fetch_outcome()

        assert outcome is not None and outcome.ok is False
        assert "パターン" in outcome.error

    @pytest.mark.asyncio
    async def test_sitemap_unreachable_records_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w = _sitemap_watcher(tmp_path)
        monkeypatch.setattr(SitemapWatcher, "_collect_all_entries", AsyncMock(return_value=[]))

        await w.fetch_articles()
        outcome = w.last_fetch_outcome()

        assert outcome is not None and outcome.ok is False


class TestPersistenceSeam:
    """`_persist_feed_health` が transport 横断 seam を使うこと。"""

    def _repo(self) -> Any:
        class _Repo:
            def __init__(self) -> None:
                self.rows: list[tuple[str, str, bool, str, int]] = []

            def upsert_source_fetch_health(
                self, outcomes: list[tuple[str, str, bool, str, int]]
            ) -> None:
                self.rows.extend(outcomes)

        return _Repo()

    def test_scraper_outcomes_are_persisted(self) -> None:
        from src.pipeline.persistence import _persist_feed_health

        class _ClusterSource:
            def last_fetch_health(self) -> list[SourceFetchOutcome]:
                return [
                    SourceFetchOutcome(LISTING_URL, "Example Blog", True, 5, ""),
                    SourceFetchOutcome(SITEMAP_URL, "Example Sitemap", False, 0, "壊れた"),
                ]

        repo = self._repo()
        _persist_feed_health(_ClusterSource(), repo)  # type: ignore[arg-type]

        assert repo.rows == [
            (LISTING_URL, "Example Blog", True, "", 5),
            (SITEMAP_URL, "Example Sitemap", False, "壊れた", 0),
        ]

    def test_legacy_last_results_still_supported(self) -> None:
        from dataclasses import dataclass

        from src.pipeline.persistence import _persist_feed_health

        @dataclass
        class _Feed:
            url: str
            name: str

        @dataclass
        class _Result:
            feed: _Feed
            articles: list[str]
            error: str | None

        class _LegacySource:
            last_results = (_Result(_Feed("https://f/rss", "F"), ["a"], None),)

        repo = self._repo()
        _persist_feed_health(_LegacySource(), repo)  # type: ignore[arg-type]
        assert repo.rows == [("https://f/rss", "F", True, "", 1)]
