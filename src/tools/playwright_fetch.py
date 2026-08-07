"""listing/sitemap 取得の第 3 段 — Playwright による JS チャレンジ突破 (raw text)。

2026-08-01 の fetch エスカレーション (bot UA → browser UA → Playwright 指紋発火) は
本文取得層 (content_extractor) にのみ実装され、その上流の sitemap/listing 取得は
UA 2 段止まりだった — ISW が Cloudflare で恒久 403 のまま残った真因。listing 層にも
同じ最終段を提供する (発火条件は呼出側: ブロック署名 status + JS チャレンジ指紋)。

設計:
- page.goto でチャレンジを解かせた後、**browser cookie (cf_clearance) を共有する
  APIRequestContext で raw text を取り直す** — XML sitemap を page.content() で読むと
  Chromium の XML viewer HTML に化けるため。取り直しも challenge なら page.content()
  に fallback (HTML listing はこちらで十分)。
- 1 時間窓の試行 cap (既定 5、env PLAYWRIGHT_LISTING_CAP)。listing は hourly ×
  ブロックされた少数ソースしか来ないため小さくてよい。cap は成功/失敗を問わず消費。
- browser は呼出ごとに起動/破棄 (常駐させない — cap 5/h なら起動コスト ~2s は許容)。
"""

from __future__ import annotations

import os
import time

from src.logging_config import get_logger
from src.tools.fetch_policy import looks_like_js_challenge

_log = get_logger(__name__)

_DEFAULT_CAP_PER_HOUR = 5
_WINDOW_SECONDS = 3600.0
_DEFAULT_NAV_TIMEOUT_MS = 30_000
_DEFAULT_POST_LOAD_WAIT_MS = 6_000

# 直近 1 時間の試行時刻 (monotonic)。常駐 process 内の簡易 sliding window。
_attempts: list[float] = []


def _cap_from_env() -> int:
    raw = os.environ.get("PLAYWRIGHT_LISTING_CAP", "").strip()
    try:
        return int(raw) if raw else _DEFAULT_CAP_PER_HOUR
    except ValueError:
        return _DEFAULT_CAP_PER_HOUR


def _within_cap() -> bool:
    now = time.monotonic()
    _attempts[:] = [t for t in _attempts if now - t < _WINDOW_SECONDS]
    if len(_attempts) >= _cap_from_env():
        return False
    _attempts.append(now)
    return True


async def fetch_text_via_playwright(
    url: str,
    *,
    user_agent: str,
    nav_timeout_ms: int = _DEFAULT_NAV_TIMEOUT_MS,
    post_load_wait_ms: int = _DEFAULT_POST_LOAD_WAIT_MS,
) -> str | None:
    """URL の raw text を Playwright 経由で取得する。失敗/cap 超過は None。"""
    if not _within_cap():
        _log.info("playwright_listing_cap_reached", url=url, cap=_cap_from_env())
        return None
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        _log.debug("playwright_not_installed")
        return None
    # 本文取得層と同じ stealth 初期化を共有 (二重管理しない)
    from src.tools.content_extractor import _STEALTH_INIT_SCRIPT

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-default-browser-check",
                    "--no-first-run",
                ],
                ignore_default_args=["--enable-automation"],
            )
            try:
                context = await browser.new_context(
                    user_agent=user_agent,
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
                context.set_default_navigation_timeout(nav_timeout_ms)
                await context.add_init_script(_STEALTH_INIT_SCRIPT)
                page = await context.new_page()
                _log.info("playwright_listing_fetch_start", url=url)
                await page.goto(url, wait_until="domcontentloaded")
                if post_load_wait_ms > 0:
                    await page.wait_for_timeout(post_load_wait_ms)
                # challenge 突破後の cookie を共有する API request で raw text を取得
                text: str | None = None
                try:
                    resp = await context.request.get(url)
                    if resp.ok:
                        text = await resp.text()
                except Exception as e:  # noqa: BLE001 — page.content() fallback へ
                    _log.debug("playwright_listing_api_get_failed", url=url, error=str(e))
                if not text or looks_like_js_challenge(text):
                    text = await page.content()
                await context.close()
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001 — 最終段の失敗は None (呼出側の通常失敗経路へ)
        _log.info("playwright_listing_fetch_failed", url=url, error=str(e)[:120])
        return None
    if not text or looks_like_js_challenge(text):
        _log.info("playwright_listing_still_challenged", url=url)
        return None
    _log.info("playwright_listing_recovered", url=url, chars=len(text))
    return text
