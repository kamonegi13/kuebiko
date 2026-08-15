"""behavior-preserving 証明: ソース loader が yaml 読み と DB 読み で完全一致すること。

同一 seed yaml から (a) flag OFF で yaml 直読み、 (b) DB に seed 後 flag ON で DB 読み
した結果が、 3 transport すべてで typed config として一致し、 かつ「enabled なソース集合
(パイプラインが実 fetch する集合)」も一致することを確認する。 routing engine の
test_equivalence_over_matrix と同思想。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.sources import source_store
from src.tools.direct_rss_source import load_feeds_config
from src.watchers.scrapers_registry import load_scrapers_config
from src.watchers.yaml_registry import load_watchers_config

_FEEDS = {
    "feeds": [
        {
            "name": "Feed Enabled",
            "url": "https://a.example/feed",
            "enabled": True,
            "folder": "news_en",
        },
        {"name": "Feed Disabled", "url": "https://b.example/feed", "enabled": False},
        {"name": "Feed NoFolder", "url": "https://c.example/feed", "enabled": True},
    ]
}
_WATCHERS = {
    "watchers": [
        {
            "name": "wat-enabled",
            "type": "sitemap",
            "enabled": True,
            "sitemap_urls": ["https://w.example/sitemap.xml"],
            "url_include_pattern": r"^https://w\.example/.+",
            "feed_title": "Watcher Enabled",
            "language": "ja",
            "max_posts_per_run": 12,
            "folder": "advisory",
        },
        {
            "name": "wat-disabled",
            "type": "sitemap",
            "enabled": False,
            "sitemap_urls": ["https://x.example/sitemap.xml"],
            "url_include_pattern": r"^https://x\.example/.+",
            "feed_title": "Watcher Disabled",
        },
    ]
}
_SCRAPERS = {
    "scrapers": [
        {
            "name": "scr-enabled",
            "enabled": True,
            "listing_url": "https://s.example/list",
            "article_link_selector": ".title a",
            "title_selector": "h1",
            "feed_title": "Scraper Enabled",
            "language": "en",
            "max_posts_per_run": 8,
            "folder": "research",
        },
        {
            "name": "scr-disabled",
            "enabled": False,
            "listing_url": "https://t.example/list",
            "article_link_selector": ".a",
            "feed_title": "Scraper Disabled",
        },
    ]
}


@pytest.fixture
def _cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    for _sub in ("sources", "cti", "delivery"):
        (cfg / _sub).mkdir(exist_ok=True)
    (cfg / "sources/feeds.yaml").write_text(
        yaml.safe_dump(_FEEDS, allow_unicode=True), encoding="utf-8"
    )
    (cfg / "sources/watchers.yaml").write_text(
        yaml.safe_dump(_WATCHERS, allow_unicode=True), encoding="utf-8"
    )
    (cfg / "sources/scrapers.yaml").write_text(
        yaml.safe_dump(_SCRAPERS, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    return cfg


def test_loader_equivalence_yaml_vs_db(_cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # (a) flag OFF = yaml 直読み (旧挙動)
    monkeypatch.setenv("SOURCES_CONFIG_DB", "0")
    feeds_yaml = load_feeds_config()
    watchers_yaml = load_watchers_config()
    scrapers_yaml = load_scrapers_config()

    # (b) 同一 yaml を DB に seed し、 flag ON = DB 読み
    monkeypatch.setenv("SOURCES_CONFIG_DB", "1")
    seeded = source_store.seed_all_if_absent()
    assert seeded == {"feeds": True, "watchers": True, "scrapers": True}
    feeds_db = load_feeds_config()
    watchers_db = load_watchers_config()
    scrapers_db = load_scrapers_config()

    # typed config が完全一致 (pydantic model __eq__ = field 比較)
    assert feeds_yaml.feeds == feeds_db.feeds
    assert watchers_yaml.watchers == watchers_db.watchers
    assert scrapers_yaml.scrapers == scrapers_db.scrapers


def test_enabled_set_equivalence(_cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # パイプラインが実際に fetch する「enabled なソース集合」が yaml/DB で一致すること
    monkeypatch.setenv("SOURCES_CONFIG_DB", "0")
    feeds_on_yaml = {f.url for f in load_feeds_config().feeds if f.enabled}
    wat_on_yaml = {w.name for w in load_watchers_config().watchers if w.enabled}
    scr_on_yaml = {s.name for s in load_scrapers_config().scrapers if s.enabled}

    monkeypatch.setenv("SOURCES_CONFIG_DB", "1")
    source_store.seed_all_if_absent()
    feeds_on_db = {f.url for f in load_feeds_config().feeds if f.enabled}
    wat_on_db = {w.name for w in load_watchers_config().watchers if w.enabled}
    scr_on_db = {s.name for s in load_scrapers_config().scrapers if s.enabled}

    assert feeds_on_yaml == feeds_on_db == {"https://a.example/feed", "https://c.example/feed"}
    assert wat_on_yaml == wat_on_db == {"wat-enabled"}
    assert scr_on_yaml == scr_on_db == {"scr-enabled"}
