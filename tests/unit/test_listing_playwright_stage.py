"""listing/sitemap 取得の Playwright 第 3 段 (監査 2026-08-01 追補)。

08-01 のエスカレーションは本文取得層のみで、上流の sitemap/listing 取得は UA 2 段
止まりだった (ISW が Cloudflare 恒久 403 のまま残った真因)。ブロック署名 +
JS チャレンジ指紋のときだけ Playwright 段が発火することを検証する
(browser 本体は monkeypatch — CI で実ブラウザは起動しない)。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from src.watchers import html_listing as hl_mod
from src.watchers import sitemap_base as sb_mod
from src.watchers.html_listing import HtmlListingWatcher
from src.watchers.sitemap_base import SitemapWatcher

_CHALLENGE_HTML = "<html><title>Just a moment...</title><body>cf-challenge</body></html>"
_SITEMAP_XML = (
    '<?xml version="1.0"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://understandingwar.org/research/some-report</loc></url>"
    "</urlset>"
)


def _resp(status: int, text: str) -> Any:
    return SimpleNamespace(status_code=status, text=text, raise_for_status=lambda: None)


def _sitemap_watcher(tmp_path: Path) -> SitemapWatcher:
    return SitemapWatcher(
        name="isw-test",
        sitemap_urls=("https://understandingwar.org/sitemap_index.xml",),
        url_include_pattern=re.compile(r"."),
        state_file=tmp_path / "seen.json",
        feed_title="ISW",
    )


@pytest.mark.asyncio
async def test_sitemap_blocked_challenge_recovers_via_playwright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_staged_get(client: Any, url: str, **kw: Any) -> tuple[Any, str]:
        return _resp(403, _CHALLENGE_HTML), "browser"

    called: dict[str, str] = {}

    async def _fake_pw(url: str, **kw: Any) -> str:
        called["url"] = url
        return _SITEMAP_XML

    monkeypatch.setattr(sb_mod, "staged_get", _fake_staged_get)
    monkeypatch.setattr("src.tools.playwright_fetch.fetch_text_via_playwright", _fake_pw)

    w = _sitemap_watcher(tmp_path)
    urls = await w._fetch_sitemap(cast(httpx.AsyncClient, SimpleNamespace()), w.sitemap_urls[0])

    assert called["url"] == w.sitemap_urls[0]
    assert urls == [("https://understandingwar.org/research/some-report", None)]


@pytest.mark.asyncio
async def test_sitemap_plain_error_does_not_fire_playwright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """指紋なしの 404 等では発火しない (cap を浪費しない)。"""

    async def _fake_staged_get(client: Any, url: str, **kw: Any) -> tuple[Any, str]:
        return _resp(404, "not found"), "bot"

    async def _boom(url: str, **kw: Any) -> str:
        raise AssertionError("Playwright 段が発火してはいけない")

    monkeypatch.setattr(sb_mod, "staged_get", _fake_staged_get)
    monkeypatch.setattr("src.tools.playwright_fetch.fetch_text_via_playwright", _boom)

    w = _sitemap_watcher(tmp_path)
    urls = await w._fetch_sitemap(cast(httpx.AsyncClient, SimpleNamespace()), w.sitemap_urls[0])
    assert urls == []


@pytest.mark.asyncio
async def test_html_listing_blocked_challenge_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_staged_get(client: Any, url: str, **kw: Any) -> tuple[Any, str]:
        return _resp(403, _CHALLENGE_HTML), "browser"

    async def _fake_pw(url: str, **kw: Any) -> str:
        return "<html><a href='/blog/x'>x</a></html>"

    monkeypatch.setattr(hl_mod, "staged_get", _fake_staged_get)
    monkeypatch.setattr("src.tools.playwright_fetch.fetch_text_via_playwright", _fake_pw)

    w = HtmlListingWatcher(
        name="t",
        listing_url="https://example.com/blog",
        article_link_selector="a",
        state_file=tmp_path / "seen.json",
        feed_title="T",
    )
    html = await w._fetch_listing()
    assert "/blog/x" in html
