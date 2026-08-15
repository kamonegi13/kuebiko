"""購読ソース 1 件の編集 (取得設定の変更) のテスト。

登録 (wizard) と lifecycle 操作の間に空いていた「後から直す」経路。
seed yaml fixture が初期値、編集結果は config_store (DB) に保存され
source_store.load_entries で観測する (test_source_rename.py と同じ構え)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.sources import source_store
from src.ui.api._source_editor import SourcePatch, get_editable, update_source


@pytest.fixture
def _cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    for _sub in ("sources", "cti", "delivery"):
        (cfg / _sub).mkdir(exist_ok=True)
    (cfg / "sources/feeds.yaml").write_text(
        yaml.safe_dump(
            {
                "feeds": [
                    {
                        "name": "Acme Feed",
                        "url": "https://acme.example/rss",
                        "enabled": True,
                        "folder": "news_sec",
                    },
                    {"name": "Other Feed", "url": "https://other.example/rss", "enabled": True},
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (cfg / "sources/scrapers.yaml").write_text(
        yaml.safe_dump(
            {
                "scrapers": [
                    {
                        "name": "acme-scraper",
                        "enabled": True,
                        "listing_url": "https://acme.example/list",
                        "article_link_selector": "a.old",
                        "feed_title": "Acme Research",
                        "max_posts_per_run": 8,
                    }
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (cfg / "sources/watchers.yaml").write_text(
        yaml.safe_dump(
            {
                "watchers": [
                    {
                        "name": "acme-watcher",
                        "type": "sitemap",
                        "enabled": True,
                        "sitemap_urls": ["https://acme.example/sitemap.xml", "https://acme/2.xml"],
                        "url_include_pattern": "^https://acme\\.example/.+",
                        "feed_title": "Acme Watcher",
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


def _rss(url: str) -> dict[str, object]:
    return next(e for e in source_store.load_entries("rss") if e.get("url") == url)


class TestGetEditable:
    def test_rss_marks_url_as_identity(self, _cfg: Path) -> None:
        src = get_editable("https://acme.example/rss")
        assert src is not None
        assert (src.display_name, src.url, src.folder) == (
            "Acme Feed",
            "https://acme.example/rss",
            "news_sec",
        )
        # rss は URL が識別子 → UI は「統計の連続性が切れる」と警告する必要がある
        assert src.url_is_identity is True
        assert src.article_link_selector is None and src.url_include_pattern is None

    def test_scraper_exposes_selector_only(self, _cfg: Path) -> None:
        src = get_editable("scraper:acme-scraper")
        assert src is not None
        assert src.article_link_selector == "a.old"
        assert src.url == "https://acme.example/list"
        assert src.url_include_pattern is None
        assert src.url_is_identity is False

    def test_sitemap_exposes_first_url_and_pattern(self, _cfg: Path) -> None:
        src = get_editable("watcher:acme-watcher")
        assert src is not None
        assert src.url == "https://acme.example/sitemap.xml"
        assert src.url_include_pattern == "^https://acme\\.example/.+"

    def test_unknown_feed_id(self, _cfg: Path) -> None:
        assert get_editable("https://nope.example/rss") is None


class TestUpdateSource:
    def test_rss_url_change_moves_identity(self, _cfg: Path) -> None:
        new_id, changed = update_source(
            "https://acme.example/rss", SourcePatch(url="https://acme.example/feed.xml")
        )
        assert (new_id, changed) == ("https://acme.example/feed.xml", True)
        entry = _rss("https://acme.example/feed.xml")
        # URL 以外は保持される (name/folder が巻き添えで消えない)
        assert entry["name"] == "Acme Feed" and entry["folder"] == "news_sec"

    def test_duplicate_url_is_rejected(self, _cfg: Path) -> None:
        with pytest.raises(ValueError, match="既に登録"):
            update_source("https://acme.example/rss", SourcePatch(url="https://other.example/rss"))
        # 衝突時は 1 件も書き換えない
        assert _rss("https://acme.example/rss")["name"] == "Acme Feed"

    def test_folder_cleared_by_empty_string(self, _cfg: Path) -> None:
        update_source("https://acme.example/rss", SourcePatch(folder=""))
        assert "folder" not in _rss("https://acme.example/rss")

    def test_no_change_reports_false(self, _cfg: Path) -> None:
        feed_id, changed = update_source(
            "https://acme.example/rss", SourcePatch(url="https://acme.example/rss")
        )
        assert (feed_id, changed) == ("https://acme.example/rss", False)

    def test_scraper_selector_and_max_posts(self, _cfg: Path) -> None:
        feed_id, changed = update_source(
            "scraper:acme-scraper",
            SourcePatch(article_link_selector="h2 > a", max_posts_per_run=12, enabled=False),
        )
        assert (feed_id, changed) == ("scraper:acme-scraper", True)
        entry = next(e for e in source_store.load_entries("html_scraper"))
        assert entry["article_link_selector"] == "h2 > a"
        assert entry["max_posts_per_run"] == 12
        assert entry["enabled"] is False

    def test_sitemap_url_replaces_first_and_keeps_rest(self, _cfg: Path) -> None:
        update_source("watcher:acme-watcher", SourcePatch(url="https://acme.example/sm2.xml"))
        entry = next(e for e in source_store.load_entries("sitemap"))
        assert entry["sitemap_urls"] == ["https://acme.example/sm2.xml", "https://acme/2.xml"]

    def test_transport_specific_field_ignored_for_rss(self, _cfg: Path) -> None:
        # scraper 専用フィールドを rss に投げても混入させない
        update_source("https://acme.example/rss", SourcePatch(article_link_selector="a"))
        assert "article_link_selector" not in _rss("https://acme.example/rss")

    def test_unknown_feed_id_raises(self, _cfg: Path) -> None:
        with pytest.raises(ValueError, match="見つかりません"):
            update_source("https://nope.example/rss", SourcePatch(folder="x"))


class TestUpdateValidation:
    """API 境界の検証 — 壊れた設定を保存させない (取得は次 run まで走らないため)。"""

    @staticmethod
    def _req(**kw: object) -> object:
        from src.ui.api._source_models import UpdateSourceRequest

        return UpdateSourceRequest(feed_id="https://acme.example/rss", **kw)  # type: ignore[arg-type]

    def _expect_400(self, **kw: object) -> str:
        from fastapi import HTTPException

        from src.ui.api.sources import _validate_update

        with pytest.raises(HTTPException) as ei:
            _validate_update(self._req(**kw))  # type: ignore[arg-type]
        assert ei.value.status_code == 400
        return str(ei.value.detail)

    def test_private_url_rejected(self) -> None:
        # SSRF: 内部ホストを購読先にできない (fetch は次 run で走るため保存時に止める)
        assert "安全でない" in self._expect_400(url="http://127.0.0.1:8001/feed")

    def test_non_http_scheme_rejected(self) -> None:
        assert "http" in self._expect_400(url="file:///etc/passwd")

    def test_bad_folder_rejected(self) -> None:
        assert "folder" in self._expect_400(folder="News EN")

    def test_max_posts_out_of_range(self) -> None:
        assert "1〜50" in self._expect_400(max_posts_per_run=999)

    def test_broken_regex_rejected(self) -> None:
        assert "パターン" in self._expect_400(url_include_pattern="^[unclosed")

    def test_empty_selector_rejected(self) -> None:
        assert "セレクタ" in self._expect_400(article_link_selector="   ")

    def test_valid_patch_passes(self) -> None:
        from src.ui.api.sources import _validate_update

        _validate_update(self._req(url="https://example.com/feed.xml", folder="news_sec"))  # type: ignore[arg-type]
