"""Playwright ベースの URL discovery → ``Article`` emit 基盤 (Phase 5T-F-2)。

設計動機:
    sitemap.xml が公開されていない / Cloudflare 配下で httpx 直アクセスが弾かれる
    サイト (例: 38 North) も URL を取得して主パイプラインに合流させたい。
    grok/fetcher.py で使用済の Playwright + state.json パターンを汎用化し、
    各 site は ``PlaywrightWatcher`` をインスタンス化するだけで済むようにする。

設計方針:
    - Browser は per-watcher で短命起動 (per-fetch_articles の 1 回)。
      Grok と違い 1 日 1 回の運用 (Phase 5T) のため persistent browser は過剰
    - state.json は ``data/playwright/<watcher_name>/state.json`` で per-watcher
      に分離 (Cookie の混在を避ける)
    - DOM から CSS selector で ``href`` を抽出 → include / exclude regex で篩う
    - seen 管理は ``SitemapWatcher`` と同 JSON 形式 (互換性)
    - ``read_state()`` は ``SitemapWatcher.read_state()`` と同 schema (UI 統一)

CLAUDE.md §4 セキュリティ整合:
    - ログに URL / count しか出さない (本文は出さない)
    - 認証情報を扱わない (公開ページ前提)

公開 API:
    - ``PlaywrightWatcher(name, target_url, ...).fetch_articles()``
    - ``init_seen()``: 初回 false-positive 防止
    - ``read_state()``: UI 表示用スナップショット
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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
from src.tools.article_model import Article
from src.tools.user_agent import browser_user_agent

_log = get_logger(__name__)

# Cloudflare の Just-a-Moment 突破には realistic な UA が必要。UA は単一ソース
# (user_agent.browser_user_agent) を呼出時に解決し、自己修復ジョブが更新する現行版を共有する。
DEFAULT_NAVIGATION_TIMEOUT_MS = 60_000
# Cloudflare challenge は通常 5 秒前後で完了する。domcontentloaded 後に
# 追加で待つことで JS 実行完了を保証する。
DEFAULT_POST_LOAD_WAIT_MS = 6_000

# navigator.webdriver / chrome / languages / plugins のパッチ (grok/fetcher.py と同じ)
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en', 'ja-JP', 'ja'],
});
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""

DEFAULT_STATE_ROOT = Path("data/playwright")


class WatcherState(BaseModel):
    """UI 表示用 watcher 状態スナップショット (SitemapWatcher と同 schema)。"""

    model_config = ConfigDict(frozen=True)

    name: str
    seen_count: int
    state_file_exists: bool
    last_modified: datetime | None
    state_path: str


@dataclass(frozen=True)
class PlaywrightWatcher:
    """Playwright で DOM から URL を抽出する汎用 watcher (Cloudflare 突破想定)。

    Attributes:
        name: watcher 識別子 (例: "38north"、kebab-case)
        target_url: URL を集める起点ページ (通常は site root / 一覧ページ)
        link_selector: ``href`` を持つ要素を選ぶ CSS selector
            (例: "article a", "h2.entry-title a")
        url_include_pattern: 含めたい記事 URL の正規表現
        seen_file: seen URL を保存する JSON ファイルパス
        feed_title: Article.feed_title に入る表示用名前
        url_exclude_pattern: 除外する URL の正規表現 (オプション)
        max_posts_per_run: 1 回の fetch で返す article 数の上限
        navigation_timeout_ms: ナビゲーション timeout
        post_load_wait_ms: domcontentloaded 後の追加待ち時間 (Cloudflare 用)
        state_root: state.json の保存先 root (per-watcher subdir 作成)
    """

    name: str
    target_url: str
    link_selector: str
    url_include_pattern: re.Pattern[str]
    seen_file: Path
    feed_title: str
    url_exclude_pattern: re.Pattern[str] | None = None
    max_posts_per_run: int = 8
    navigation_timeout_ms: int = DEFAULT_NAVIGATION_TIMEOUT_MS
    post_load_wait_ms: int = DEFAULT_POST_LOAD_WAIT_MS
    state_root: Path = DEFAULT_STATE_ROOT

    @property
    def _state_path(self) -> Path:
        """Playwright の Cookie / localStorage を保存する state.json のパス。"""
        return self.state_root / self.name / "state.json"

    # ---- URL 取得 ----

    async def _collect_all_urls(self) -> list[str]:
        """Playwright で ``target_url`` にアクセスし、``link_selector`` 経由で
        href 属性を収集する。Cloudflare wait + stealth init を含む。
        """
        playwright: Playwright | None = None
        browser: Browser | None = None
        context: BrowserContext | None = None
        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-default-browser-check",
                    "--no-first-run",
                ],
                ignore_default_args=["--enable-automation"],
            )

            context_kwargs: dict[str, Any] = {
                "user_agent": browser_user_agent(),
                "viewport": {"width": 1280, "height": 800},
                "locale": "en-US",
            }
            if self._state_path.exists():
                context_kwargs["storage_state"] = str(self._state_path)

            context = await browser.new_context(**context_kwargs)
            context.set_default_navigation_timeout(self.navigation_timeout_ms)
            await context.add_init_script(_STEALTH_INIT_SCRIPT)
            page = await context.new_page()

            _log.info("playwright_watcher_fetch_start", watcher=self.name, url=self.target_url)
            try:
                response = await page.goto(self.target_url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError as e:
                _log.warning(
                    "playwright_watcher_timeout",
                    watcher=self.name,
                    url=self.target_url,
                    error=str(e),
                )
                return []

            initial_status = response.status if response is not None else 0

            # Cloudflare の interstitial challenge は HTTP 403 で HTML challenge を返し、
            # JS が実行されると 5-10 秒で実ページにリダイレクトされる。
            # 403 即時 bail せず、selector が現れるまで wait することで突破を試みる。
            if self.post_load_wait_ms > 0:
                await page.wait_for_timeout(self.post_load_wait_ms)

            # link_selector で href が取れるか試す。Cloudflare challenge ページなら
            # まだ 0 件のため、少し待って selector wait を試す。
            try:
                await page.wait_for_selector(
                    self.link_selector,
                    timeout=10_000,
                    state="attached",
                )
            except PlaywrightTimeoutError:
                _log.warning(
                    "playwright_watcher_selector_not_found",
                    watcher=self.name,
                    url=self.target_url,
                    initial_status=initial_status,
                    final_url=page.url,
                )
                return []

            # DOM の href を全て取り出す。eval_on_selector_all は string[] を返す。
            hrefs: list[str] = await page.eval_on_selector_all(
                self.link_selector,
                "els => els.map(el => el.href).filter(Boolean)",
            )

            # state.json を保存 (Cloudflare clearance token 等を次回再利用)
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(self._state_path))

            _log.info(
                "playwright_watcher_fetch_done",
                watcher=self.name,
                initial_status=initial_status,
                final_url=page.url,
                href_count=len(hrefs),
            )
            return hrefs
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()
            if playwright is not None:
                await playwright.stop()

    def _filter_urls(self, urls: list[str]) -> list[str]:
        """include / exclude regex で URL を篩い、重複排除する (順序保存)。"""
        seen_dedup: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u in seen_dedup:
                continue
            if not self.url_include_pattern.search(u):
                continue
            if self.url_exclude_pattern and self.url_exclude_pattern.search(u):
                continue
            seen_dedup.add(u)
            out.append(u)
        return out

    # ---- seen state ----

    def _load_seen(self) -> set[str]:
        if not self.seen_file.exists():
            return set()
        try:
            data = json.loads(self.seen_file.read_text(encoding="utf-8"))
            return set(data.get("seen_urls", []))
        except (json.JSONDecodeError, OSError) as e:
            _log.warning("playwright_seen_load_failed", watcher=self.name, error=str(e))
            return set()

    def _save_seen(self, urls: set[str]) -> None:
        self.seen_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"seen_urls": sorted(urls)}
        tmp = self.seen_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.seen_file)

    # ---- Article 変換 ----

    def _url_to_article(self, url: str) -> Article:
        article_id = f"{self.name}:{hashlib.sha1(url.encode()).hexdigest()[:16]}"  # noqa: S324
        slug = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
        title = slug[:200] if slug else url
        published = datetime.now().astimezone()
        return Article(
            id=article_id,
            title=title,
            url=url,
            summary_html="",
            author=None,
            published=published,
            feed_title=self.feed_title,
            feed_url=self.target_url,
        )

    # ---- 公開 API ----

    async def fetch_articles(self, max_count: int | None = None) -> list[Article]:
        """新規 URL を ``Article`` として返す。seen は fetch 時に更新する。"""
        cap = min(max_count or self.max_posts_per_run, self.max_posts_per_run)

        all_urls = await self._collect_all_urls()
        if not all_urls:
            _log.warning("playwright_no_urls", watcher=self.name)
            return []

        filtered = self._filter_urls(all_urls)
        seen = self._load_seen()
        new_urls = [u for u in filtered if u not in seen]

        _log.info(
            "playwright_watcher_filter",
            watcher=self.name,
            total=len(all_urls),
            filtered=len(filtered),
            seen=len(seen),
            new=len(new_urls),
        )

        if not new_urls:
            return []

        if len(new_urls) > cap:
            _log.warning(
                "playwright_too_many_new_capping",
                watcher=self.name,
                new=len(new_urls),
                cap=cap,
            )
            new_urls = new_urls[:cap]

        seen.update(new_urls)
        self._save_seen(seen)

        return [self._url_to_article(u) for u in new_urls]

    async def init_seen(self) -> int:
        """現状の全 URL を seen として記録する (初回 false-positive 防止)。"""
        all_urls = await self._collect_all_urls()
        filtered = self._filter_urls(all_urls)
        urls = set(filtered)
        self._save_seen(urls)
        _log.info("playwright_init", watcher=self.name, marked_seen=len(urls))
        return len(urls)

    def read_state(self) -> WatcherState:
        """状態ファイルを読んで UI 表示用スナップショットを返す。"""
        if not self.seen_file.exists():
            return WatcherState(
                name=self.name,
                seen_count=0,
                state_file_exists=False,
                last_modified=None,
                state_path=str(self.seen_file),
            )
        try:
            data = json.loads(self.seen_file.read_text(encoding="utf-8"))
            seen_count = len(data.get("seen_urls", []))
        except (json.JSONDecodeError, OSError) as e:
            _log.warning("playwright_state_read_failed", watcher=self.name, error=str(e))
            seen_count = 0
        mtime = datetime.fromtimestamp(self.seen_file.stat().st_mtime).astimezone()
        return WatcherState(
            name=self.name,
            seen_count=seen_count,
            state_file_exists=True,
            last_modified=mtime,
            state_path=str(self.seen_file),
        )
