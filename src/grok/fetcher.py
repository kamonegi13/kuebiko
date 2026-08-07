"""Grok レポート取得 (Phase 2: Playwright で grok.com から HTML を取得)。

Playwright Async API を使い、ヘッドレス Chromium でレポート URL に
アクセスして本文 HTML を取得する。

セッション永続化:
- ``data/playwright/state.json`` (volume mount でホストに保存される)
- 一度ログインしたら同じ session.json を再利用するため、Cookie / ローカル
  ストレージが保存される
- 初回ログインは ``scripts/grok_login.py`` (CLI、headed) で行う
- セッションが切れたら 401 / login redirect を検出して上位に伝える

設計方針:
- Browser インスタンスを使い回し、Context + Page は短命にする (リソース節約)
- レポート本文の抽出は trafilatura に任せる (このモジュールは HTML 取得まで)
- timeout は十分長めに (Grok レポート生成は時間がかかる)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import TracebackType
from typing import Self

from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger
from src.tools.user_agent import browser_user_agent

_log = get_logger(__name__)

DEFAULT_STATE_PATH = Path("data/playwright/state.json")
DEFAULT_NAVIGATION_TIMEOUT_MS = 60_000
# SSE / WebSocket 常時接続の SPA (grok.com) では networkidle は永遠に発火しない。
# domcontentloaded を起点にし、hydration / 認証チェック / 初回 chat fetch が
# 走るまでの猶予として post_load_wait_ms ぶん追加で待つ。
DEFAULT_WAIT_UNTIL = "domcontentloaded"
DEFAULT_POST_LOAD_WAIT_MS = 4_000

# Grok の chat ページから本文 (アシスタント応答) を抽出するための CSS selector。
# 上から順に試して最初にマッチしたものを採用する。
# - [class*='markdown']: Grok の markdown-rendered 応答ブロック (一番きれい)
# - .prose: tailwind typography の wrapper (思考プレフィックスを含むことあり)
# - main: フォールバック (sidebar/nav を含むためノイジー)
DEFAULT_BODY_SELECTORS: tuple[str, ...] = (
    "[class*='markdown']",
    ".prose",
    "main",
)

# navigator.webdriver を隠して自動化検知を回避するパッチ。
# scripts/grok_login.py と揃えておく。
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {
    get: () => ['ja-JP', 'ja', 'en-US', 'en'],
});
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""

# ログイン誘導ページの判定マーカー (URL パスに含まれる場合)
LOGIN_REDIRECT_MARKERS: tuple[str, ...] = (
    "/login",
    "/i/flow/login",
    "/oauth/authorize",
    "/sign-in",
    "/auth",
)


# ---------- Models ----------


class GrokFetchResult(BaseModel):
    """Grok レポート取得の結果。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    url: str
    final_url: str
    html: str = ""
    # SPA hydration 後に DOM から抽出したアシスタント応答テキスト。
    # trafilatura は SSR シェルから本文を取れないため、この値を要約に使う。
    body_text: str = ""
    title: str | None = None
    success: bool
    failure_reason: str | None = None


class GrokFetchError(Exception):
    """Grok 取得の汎用エラー。"""


class GrokSessionExpiredError(GrokFetchError):
    """セッション切れ (要再ログイン)。"""


class GrokTimeoutError(GrokFetchError):
    """ナビゲーション / ロード タイムアウト。"""


# ---------- Fetcher ----------


class GrokFetcher:
    """Playwright ベースの Grok レポート取得クライアント。

    使い方::

        async with GrokFetcher() as fetcher:
            result = await fetcher.fetch("https://grok.com/share/abc123")
            if result.success:
                summary = await content_extractor.extract_html(result.html)
    """

    def __init__(
        self,
        state_path: Path | str = DEFAULT_STATE_PATH,
        *,
        navigation_timeout_ms: int = DEFAULT_NAVIGATION_TIMEOUT_MS,
        user_agent: str | None = None,
        headless: bool = True,
        wait_until: str = DEFAULT_WAIT_UNTIL,
        post_load_wait_ms: int = DEFAULT_POST_LOAD_WAIT_MS,
        body_selectors: tuple[str, ...] = DEFAULT_BODY_SELECTORS,
    ) -> None:
        self._state_path = Path(state_path)
        self._navigation_timeout = navigation_timeout_ms
        # UA は単一ソース (user_agent.browser_user_agent) を構築時に解決 (2026-07-27)。
        self._user_agent = user_agent or browser_user_agent()
        self._headless = headless
        self._wait_until = wait_until
        self._post_load_wait_ms = post_load_wait_ms
        self._body_selectors = body_selectors
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ----- lifecycle -----

    async def start(self) -> None:
        if self._playwright is not None:
            return
        self._playwright = await async_playwright().start()
        # Phase 2 改善: ボット検知を回避するため自動化フラグを抑制する
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-default-browser-check",
                "--no-first-run",
            ],
            ignore_default_args=["--enable-automation"],
        )
        _log.info("grok_browser_started", headless=self._headless)

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        _log.info("grok_browser_closed")

    # ----- public API -----

    async def fetch(self, url: str) -> GrokFetchResult:
        """指定 URL を取得し、HTML + 最終 URL + タイトルを返す。"""
        if self._browser is None:
            raise GrokFetchError("start() が完了していません")

        async with self._lock:
            return await self._fetch_locked(url)

    async def has_session(self) -> bool:
        """セッション state.json が存在するか確認する。"""
        return self._state_path.exists()

    # ----- internal -----

    async def _fetch_locked(self, url: str) -> GrokFetchResult:
        assert self._browser is not None
        # Playwright の new_context は kwargs を多数受け取るため、Any 経由で渡す
        # (mypy の精密な kwargs 型推論を回避)。
        from typing import Any as _Any

        context_kwargs: dict[str, _Any] = {
            "user_agent": self._user_agent,
            "viewport": {"width": 1280, "height": 800},
        }
        if self._state_path.exists():
            context_kwargs["storage_state"] = str(self._state_path)

        context: BrowserContext | None = None
        try:
            context = await self._browser.new_context(**context_kwargs)
            context.set_default_navigation_timeout(self._navigation_timeout)
            await context.add_init_script(_STEALTH_INIT_SCRIPT)
            page = await context.new_page()

            _log.info("grok_fetch_start", url=url, wait_until=self._wait_until)
            try:
                response = await page.goto(url, wait_until=self._wait_until)  # type: ignore[arg-type]
            except PlaywrightTimeoutError as e:
                return GrokFetchResult(
                    url=url,
                    final_url=url,
                    success=False,
                    failure_reason=f"timeout: {e}",
                )

            # SPA のため hydration / 認証チェック / 初回 chat fetch が
            # 走るまで追加で待つ。ここで本文 DOM が出来上がる。
            if self._post_load_wait_ms > 0:
                await page.wait_for_timeout(self._post_load_wait_ms)

            final_url = page.url
            if _looks_like_login_redirect(final_url):
                return GrokFetchResult(
                    url=url,
                    final_url=final_url,
                    success=False,
                    failure_reason="session_expired",
                )

            status_code = response.status if response is not None else 0
            # Phase 5P: 401 はセッション切れとして login redirect と同じ
            # failure_reason に倒し、運用側 (Web UI / scripts/grok_login.py)
            # で同じ復旧フローに乗せられるようにする。
            if status_code == 401:
                _log.warning(
                    "grok_session_expired_401",
                    url=url,
                    final_url=final_url,
                )
                return GrokFetchResult(
                    url=url,
                    final_url=final_url,
                    success=False,
                    failure_reason="session_expired",
                )
            if status_code >= 400:
                return GrokFetchResult(
                    url=url,
                    final_url=final_url,
                    success=False,
                    failure_reason=f"http_error_{status_code}",
                )

            html = await page.content()
            title = await page.title()
            body_text = await self._extract_body_text(page)

            # Phase Diamond Grok 再設計: JSONL output は code block でレンダリングされ、
            # markdown narrative より hydration に時間がかかる。body が空 or 極端に
            # 短い場合、追加で 8 秒待って再抽出する (合計最大 12 秒)。
            if len(body_text) < 100:
                _log.info(
                    "grok_body_short_retry",
                    url=url,
                    initial_length=len(body_text),
                )
                await page.wait_for_timeout(8000)
                html = await page.content()
                body_text = await self._extract_body_text(page)
                _log.info(
                    "grok_body_after_retry",
                    url=url,
                    final_length=len(body_text),
                )

            _log.info(
                "grok_fetch_success",
                url=url,
                final_url=final_url,
                status=status_code,
                html_length=len(html),
                body_length=len(body_text),
            )
            return GrokFetchResult(
                url=url,
                final_url=final_url,
                html=html,
                body_text=body_text,
                title=title or None,
                success=True,
            )
        finally:
            if context is not None:
                await context.close()

    async def _extract_body_text(self, page: object) -> str:
        """DOM から hydration 済みの本文 (アシスタント応答) を抽出する。

        ``self._body_selectors`` を上から試し、最初にマッチした selector の
        全要素 inner_text を改行区切りで結合して返す。マッチなしなら空文字列。
        """
        for selector in self._body_selectors:
            try:
                locator = page.locator(selector)  # type: ignore[attr-defined]
                count = await locator.count()
            except Exception as e:  # noqa: BLE001
                _log.warning("grok_body_selector_failed", selector=selector, error=str(e))
                continue
            if count == 0:
                continue
            try:
                texts = await locator.all_inner_texts()
            except Exception as e:  # noqa: BLE001
                _log.warning("grok_body_text_failed", selector=selector, error=str(e))
                continue
            joined = "\n\n".join(t.strip() for t in texts if t and t.strip())
            if joined:
                _log.info(
                    "grok_body_extracted",
                    selector=selector,
                    matches=count,
                    length=len(joined),
                )
                return joined
        return ""


# ---------- helpers ----------


def _looks_like_login_redirect(url: str) -> bool:
    """URL がログインページへのリダイレクトかどうか判定する。"""
    lowered = url.lower()
    return any(marker in lowered for marker in LOGIN_REDIRECT_MARKERS)
