"""Playwright で URL を描画し post-render HTML を返す共用ヘルパ。

source wizard の visual picker が JS-SPA サイトを扱うために使う。
chromium launch / context のパターンは ``watchers/playwright_base.py`` を踏襲。

SSRF 二重防御: headless browser はページ自身のリクエストで内部サービスへ
到達しうるため、``context.route`` で全 subresource/navigation の host を
解決し public でなければ abort する (CLAUDE.md §4)。
"""

from __future__ import annotations

import asyncio
import socket
from functools import lru_cache
from urllib.parse import urlparse

import structlog

from src.tools.url_guard import is_public_ip

_log = structlog.get_logger(__name__)

RENDER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_NAV_TIMEOUT_MS = 25_000
DEFAULT_POST_LOAD_WAIT_MS = 1_500
MAX_CONCURRENT_RENDERS = 2

# 同時描画数を制限 (browser は重く、無制限だとメモリ枯渇する)。
_render_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RENDERS)


@lru_cache(maxsize=512)
def _host_is_public(host: str) -> bool:
    """host (IP literal or 名前) が public に解決されるか。解決失敗は False。"""
    if is_public_ip(host):  # 既に IP literal の場合
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    ips = [str(info[4][0]) for info in infos]
    return bool(ips) and all(is_public_ip(ip) for ip in ips)


def request_allowed(request_url: str) -> bool:
    """Playwright route handler 用: この URL への subrequest を許可してよいか。

    http(s) 以外、または private/metadata host への到達は False。
    """
    parsed = urlparse(request_url)
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    return _host_is_public(host)


async def render_html(
    url: str,
    *,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    post_load_wait_ms: int = DEFAULT_POST_LOAD_WAIT_MS,
) -> str:
    """url を Playwright で描画し、描画後の DOM HTML を返す。

    呼び出し側は事前に ``url_guard.assert_safe_public_url(url)`` を済ませること
    (この関数は subresource の SSRF のみ route で防御する)。
    """
    # 遅延 import (起動コスト回避)
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    async with _render_semaphore:
        playwright = None
        browser = None
        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--no-first-run",
                ],
                ignore_default_args=["--enable-automation"],
            )
            context = await browser.new_context(
                user_agent=RENDER_USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            context.set_default_navigation_timeout(nav_timeout_ms)

            # SSRF 二重防御: private/metadata host への subrequest を abort。
            async def _route_guard(route: object, request: object) -> None:
                req_url = getattr(request, "url", "")
                if request_allowed(req_url):
                    await route.continue_()  # type: ignore[attr-defined]
                else:
                    _log.warning("page_render_blocked_subrequest", url=req_url)
                    await route.abort()  # type: ignore[attr-defined]

            await context.route("**/*", _route_guard)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError as e:
                _log.warning("page_render_nav_timeout", url=url, error=str(e))
            if post_load_wait_ms > 0:
                await page.wait_for_timeout(post_load_wait_ms)
            html: str = await page.content()
            _log.info("page_render_done", url=url, final_url=page.url, html_len=len(html))
            return html
        finally:
            if browser is not None:
                await browser.close()
            if playwright is not None:
                await playwright.stop()
