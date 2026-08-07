"""統合ソース管理 (_source_manager) の unit test。

DB SSoT 化後: seed yaml fixture が初期値、 mutation は config_store (DB) に保存される。
観測は list_sources() (DB-first) で行う。 pipelines.yaml の cluster (非移行) は触られない
ことを直接確認する。 SQLite fallback を使うため tmp に data/ を用意し chdir する。
"""

from pathlib import Path

import pytest
import yaml

from src.ui.api import _source_manager as sm

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
  feed_title: Analysis | Merics
"""
_WATCHERS = """\
watchers:
  - name: enisa
    type: sitemap
    enabled: false
    sitemap_urls: [https://www.enisa.europa.eu/sitemap.xml]
    feed_title: ENISA
"""
_FEEDS = """\
feeds:
- name: bleeping
  url: https://www.bleepingcomputer.com/feed/
  enabled: true
  folder: news_en
"""


@pytest.fixture
def _cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "pipelines.yaml").write_text(_PIPELINES, encoding="utf-8")
    (cfg / "scrapers.yaml").write_text(_SCRAPERS, encoding="utf-8")
    (cfg / "watchers.yaml").write_text(_WATCHERS, encoding="utf-8")
    (cfg / "feeds.yaml").write_text(_FEEDS, encoding="utf-8")
    (tmp_path / "data").mkdir()  # SQLite fallback (config_store) の置き場
    monkeypatch.chdir(tmp_path)
    return cfg


def _by_id() -> dict[str, sm.ManagedSource]:
    return {s.feed_id: s for s in sm.list_sources()}


def test_list_includes_disabled(_cfg: Path) -> None:
    by_id = _by_id()
    assert by_id["scraper:merics-org"].enabled is True
    assert by_id["watcher:enisa"].enabled is False  # 無効も列挙される
    assert by_id["watcher:enisa"].transport == "sitemap"
    assert by_id["https://www.bleepingcomputer.com/feed/"].folder == "news_en"


def test_bulk_disable_sets_entry_only(_cfg: Path) -> None:
    # 統合再設計: ソースは entry.enabled が単一 SSoT (DB)。 cluster (pipelines.yaml) は
    # 触らない (web_scraper_cluster が registry から auto-collect するため)。
    affected, _ = sm.bulk(["scraper:merics-org"], "disable")
    assert affected == 1
    assert _by_id()["scraper:merics-org"].enabled is False
    # pipelines.yaml の cluster member は変更されない (移行対象外 + yaml ソースは列挙されない設計)
    cluster = yaml.safe_load((_cfg / "pipelines.yaml").read_text())["pipelines"][0]
    member = next(s for s in cluster["source"]["scrapers"] if s["name"] == "merics-org")
    assert member["enabled"] is True  # 触られていない


def test_bulk_enable_watcher(_cfg: Path) -> None:
    affected, _ = sm.bulk(["watcher:enisa"], "enable")
    assert affected == 1
    assert _by_id()["watcher:enisa"].enabled is True


def test_bulk_delete_mixed(_cfg: Path) -> None:
    affected, _ = sm.bulk(
        ["scraper:merics-org", "https://www.bleepingcomputer.com/feed/"],
        "delete",
    )
    assert affected == 2
    ids = _by_id()
    assert "scraper:merics-org" not in ids
    assert "https://www.bleepingcomputer.com/feed/" not in ids


def test_bulk_noop_returns_zero(_cfg: Path) -> None:
    affected, commit = sm.bulk(["scraper:unknown"], "disable")
    assert affected == 0
    assert commit is None


def test_set_folder_all_transports(_cfg: Path) -> None:
    affected, _ = sm.set_folder(
        ["scraper:merics-org", "https://www.bleepingcomputer.com/feed/"],
        "thinktank",
    )
    assert affected == 2
    ids = _by_id()
    assert ids["scraper:merics-org"].folder == "thinktank"
    assert ids["https://www.bleepingcomputer.com/feed/"].folder == "thinktank"


def test_set_folder_clear(_cfg: Path) -> None:
    sm.set_folder(["scraper:merics-org"], "thinktank")
    affected, _ = sm.set_folder(["scraper:merics-org"], "")  # 解除
    assert affected == 1
    assert _by_id()["scraper:merics-org"].folder == ""
