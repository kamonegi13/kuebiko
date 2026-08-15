"""unregister_source (全 transport 削除) の unit test。

DB SSoT 化後: source entry の削除は config_store (DB) に反映される (観測は
source_store.load_entries)。 web-scraper-watchers cluster の宣言 (pipelines.yaml) は
移行対象外なので従来通り yaml から除去される。 SQLite fallback 用に tmp に data/ を置く。
"""

from pathlib import Path

import pytest
import yaml

from src.sources import source_store
from src.ui.api._source_writer import unregister_source

_PIPELINES = """\
pipelines:
- name: web-scraper-watchers
  source:
    type: web_scraper_cluster
    scrapers:
    - name: enisa
      enabled: true
    - name: merics-org
      enabled: true
"""

_SCRAPERS = """\
scrapers:
- name: merics-org
  enabled: true
  listing_url: https://merics.org/en/analysis
  article_link_selector: .field--name-title a
  feed_title: Merics
"""

_WATCHERS = """\
watchers:
  - name: enisa
    type: sitemap
    enabled: true
    sitemap_urls: [https://www.enisa.europa.eu/sitemap.xml]
    url_include_pattern: ^https://www\\.enisa\\.europa\\.eu/.+
    feed_title: ENISA
"""

_FEEDS = """\
feeds:
- name: bleeping
  url: https://www.bleepingcomputer.com/feed/
  enabled: true
- name: krebs
  url: https://krebsonsecurity.com/feed/
  enabled: true
"""


@pytest.fixture
def _cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    for _sub in ("sources", "cti", "delivery"):
        (cfg / _sub).mkdir(exist_ok=True)
    (cfg / "pipelines.yaml").write_text(_PIPELINES, encoding="utf-8")
    (cfg / "sources/scrapers.yaml").write_text(_SCRAPERS, encoding="utf-8")
    (cfg / "sources/watchers.yaml").write_text(_WATCHERS, encoding="utf-8")
    (cfg / "sources/feeds.yaml").write_text(_FEEDS, encoding="utf-8")
    (tmp_path / "data").mkdir()  # SQLite fallback (config_store) の置き場
    monkeypatch.chdir(tmp_path)
    return cfg


def test_delete_scraper_removes_entry_and_cluster(_cfg: Path) -> None:
    removed, _commit = unregister_source("scraper:merics-org")
    assert removed is True
    scrapers = source_store.load_entries("html_scraper")
    assert all(s["name"] != "merics-org" for s in scrapers)
    cluster = yaml.safe_load((_cfg / "pipelines.yaml").read_text())["pipelines"][0]
    names = [s["name"] for s in cluster["source"]["scrapers"]]
    assert "merics-org" not in names
    assert "enisa" in names  # 他は残る


def test_delete_watcher_removes_entry_and_cluster(_cfg: Path) -> None:
    removed, _commit = unregister_source("watcher:enisa")
    assert removed is True
    watchers = source_store.load_entries("sitemap")
    assert all(w["name"] != "enisa" for w in watchers)
    cluster = yaml.safe_load((_cfg / "pipelines.yaml").read_text())["pipelines"][0]
    names = [s["name"] for s in cluster["source"]["scrapers"]]
    assert "enisa" not in names


def test_delete_rss_feed_by_url(_cfg: Path) -> None:
    removed, _commit = unregister_source("https://krebsonsecurity.com/feed/")
    assert removed is True
    urls = [f["url"] for f in source_store.load_entries("rss")]
    assert "https://krebsonsecurity.com/feed/" not in urls
    assert "https://www.bleepingcomputer.com/feed/" in urls


def test_delete_unknown_returns_false(_cfg: Path) -> None:
    assert unregister_source("scraper:does-not-exist") == (False, None)
    assert unregister_source("https://unknown.example/feed") == (False, None)
