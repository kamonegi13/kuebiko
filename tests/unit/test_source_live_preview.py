"""live preview の feed_id dispatch / config 解決ロジックの unit test。

実 network fetch は行わず、feed_id prefix によるルーティングと _find_entry の
yaml パースを検証する。
"""

from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from src.ui.api import _source_live_preview as lp
from src.ui.api._source_models import LivePreviewResponse


def test_find_entry_parses_named_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # _find_entry は SSoT (source_store) 経由。 flag OFF で seed yaml を直読みさせる。
    monkeypatch.setenv("SOURCES_CONFIG_DB", "0")
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "scrapers.yaml").write_text(
        "scrapers:\n- name: merics-org\n  listing_url: https://merics.org/en/analysis\n"
        "  article_link_selector: .field--name-title a\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    e = lp._find_entry("html_scraper", "merics-org")
    assert e is not None
    assert e["listing_url"] == "https://merics.org/en/analysis"


def test_find_entry_missing_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCES_CONFIG_DB", "0")
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "scrapers.yaml").write_text("scrapers:\n- name: other\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert lp._find_entry("html_scraper", "merics-org") is None
    # ファイルが無い transport も None (空 list)
    assert lp._find_entry("sitemap", "x") is None


@pytest.mark.asyncio
async def test_dispatch_scraper(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {}

    async def fake_scraper(name: str) -> LivePreviewResponse:
        called["name"] = name
        return LivePreviewResponse(ok=True, kind="html_scraper")

    monkeypatch.setattr(lp, "_preview_scraper", fake_scraper)
    res = await lp.preview_subscription("scraper:merics-org")
    assert res.kind == "html_scraper"
    assert called["name"] == "merics-org"


@pytest.mark.asyncio
async def test_dispatch_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {}

    def fake_sitemap(name: str) -> LivePreviewResponse:
        called["name"] = name
        return LivePreviewResponse(ok=True, kind="sitemap")

    monkeypatch.setattr(lp, "_preview_sitemap", fake_sitemap)
    res = await lp.preview_subscription("watcher:enisa")
    assert res.kind == "sitemap"
    assert called["name"] == "enisa"


def test_parse_sitemap_lastmod() -> None:
    xml = b"""<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://x/a.html</loc><lastmod>2026-05-01T10:00:00+09:00</lastmod></url>
      <url><loc>https://x/b.html</loc><lastmod>2026-05-20T10:00:00+09:00</lastmod></url>
      <url><loc>https://x/c.html</loc></url>
    </urlset>"""
    locs = lp._parse_sitemap_locs_with_lastmod(xml)
    assert len(locs) == 3
    by_url = dict(locs)
    assert by_url["https://x/a.html"] is not None
    assert by_url["https://x/c.html"] is None
    # b は a より新しい
    assert cast(datetime, by_url["https://x/b.html"]) > by_url["https://x/a.html"]


def test_sitemap_preview_sorts_newest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # evergreen (古い) ページが document 先頭でも、最新が上に来ること
    xml = b"""<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://www.ipa.go.jp/security/vuln/old.html</loc>
        <lastmod>2019-01-01T00:00:00+09:00</lastmod></url>
      <url><loc>https://www.ipa.go.jp/security/announce/latest.html</loc>
        <lastmod>2026-05-25T00:00:00+09:00</lastmod></url>
    </urlset>"""
    monkeypatch.setattr(
        lp,
        "_find_entry",
        lambda *a, **k: {
            "sitemap_urls": ["https://www.ipa.go.jp/sitemap.xml"],
            "url_include_pattern": r"^https://www\.ipa\.go\.jp/security/.+\.html",
        },
    )
    monkeypatch.setattr(lp, "fetch_bytes", lambda *a, **k: (200, xml, {}, "bot"))
    monkeypatch.setattr(lp, "assert_safe_public_url", lambda *a, **k: None)
    monkeypatch.setattr(lp, "_fetch_page_title", lambda u: None)  # title fetch skip
    res = lp._preview_sitemap("ipa")
    assert res.ok
    assert res.items[0].url.endswith("latest.html")  # 最新が先頭


@pytest.mark.asyncio
async def test_dispatch_rss_for_bare_url(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {}

    def fake_rss(url: str) -> LivePreviewResponse:
        called["url"] = url
        return LivePreviewResponse(ok=True, kind="rss")

    monkeypatch.setattr(lp, "_preview_rss", fake_rss)
    res = await lp.preview_subscription("https://example.com/feed/")
    assert res.kind == "rss"
    assert called["url"] == "https://example.com/feed/"
