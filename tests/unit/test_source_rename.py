"""購読ソースの表示名変更 (rename_source) のテスト (DB SSoT 化後)。

seed yaml fixture が初期値、 rename は config_store (DB) に保存され source_store.load_entries
で観測する。 表示名フィールドは transport で異なる (rss=name / sitemap・html_scraper=feed_title)
が kebab の内部 ID(name) は不変。 SQLite fallback 用に tmp に data/ を置く。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.sources import source_store
from src.ui.api import _source_manager as sm


@pytest.fixture
def _cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "feeds.yaml").write_text(
        yaml.safe_dump(
            {"feeds": [{"name": "Old RSS Name", "url": "https://e/rss", "enabled": True}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (cfg / "scrapers.yaml").write_text(
        yaml.safe_dump(
            {
                "scrapers": [
                    {
                        "name": "acme-scraper",
                        "enabled": True,
                        "listing_url": "https://e/list",
                        "article_link_selector": "a",
                        "feed_title": "Old Scraper",
                    }
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (cfg / "watchers.yaml").write_text(
        yaml.safe_dump(
            {
                "watchers": [
                    {
                        "name": "acme-watcher",
                        "type": "sitemap",
                        "enabled": True,
                        "sitemap_urls": ["https://e/sitemap.xml"],
                        "url_include_pattern": "^https://e/.+",
                        "feed_title": "Old Watcher",
                    }
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()  # SQLite fallback (config_store) の置き場
    monkeypatch.chdir(tmp_path)
    return cfg


class TestRenameSource:
    def test_rss_updates_name(self, _cfg: Path) -> None:
        affected, commit = sm.rename_source("https://e/rss", "新しい RSS 名")
        assert affected == 1
        assert commit is None  # git は触らない (DB SSoT)
        assert source_store.load_entries("rss")[0]["name"] == "新しい RSS 名"

    def test_scraper_updates_feed_title_not_id(self, _cfg: Path) -> None:
        affected, _ = sm.rename_source("scraper:acme-scraper", "新スクレイパ表示名")
        assert affected == 1
        entry = source_store.load_entries("html_scraper")[0]
        assert entry["feed_title"] == "新スクレイパ表示名"
        assert entry["name"] == "acme-scraper"  # kebab ID は不変

    def test_watcher_updates_feed_title(self, _cfg: Path) -> None:
        affected, _ = sm.rename_source("watcher:acme-watcher", "新ウォッチャ表示名")
        assert affected == 1
        assert source_store.load_entries("sitemap")[0]["feed_title"] == "新ウォッチャ表示名"

    def test_unknown_feed_id_returns_zero(self, _cfg: Path) -> None:
        affected, commit = sm.rename_source("https://e/does-not-exist", "x")
        assert affected == 0
        assert commit is None

    def test_empty_display_name_raises(self, _cfg: Path) -> None:
        with pytest.raises(ValueError, match="空"):
            sm.rename_source("https://e/rss", "   ")
