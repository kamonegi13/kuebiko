"""HTML listing page から CSS selector で article URL を抽出する watcher (Phase E-3)。

設計動機:
    RSS / sitemap を持たない CTI 一次ソース (政府 advisory / 研究機関 / シンクタンク) を
    監視するため、 listing page (例 ``/news/`` ``/advisories/``) を fetch して
    BeautifulSoup .select() で article link を取得する。

設計方針:
    - SitemapWatcher と同 interface (``fetch_articles`` / ``init_seen`` /
      ``read_state``) を提供し、 ``WebScraperClusterSource`` から透過利用可能
    - selectors は ``config/scrapers.yaml`` で declarative 宣言
    - seen 状態は ``data/<name>_seen.json`` で SitemapWatcher と同一 schema
    - 本文と公開日は後段 trafilatura が URL から取得 (per-article LLM 投入時)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx

from src.logging_config import get_logger
from src.tools.article_model import Article
from src.tools.fetch_policy import HTML_ACCEPT, staged_get
from src.tools.source_fetch_outcome import FetchObservation, SourceFetchOutcome
from src.watchers.sitemap_base import WatcherState

_log = get_logger(__name__)

DEFAULT_USER_AGENT = "kuebiko/1.0 (+html-listing-watcher)"
DEFAULT_HTTP_TIMEOUT = 30.0
DEFAULT_MAX_POSTS_PER_RUN = 8


@dataclass(frozen=True)
class HtmlListingWatcher:
    """listing page + CSS selector ベースの HTML scraper watcher。

    Attributes:
        name: watcher 識別子 (kebab-case)
        listing_url: 記事一覧 page の URL
        article_link_selector: 記事 link を抽出する CSS selector (BeautifulSoup .select())
        title_selector: article element 内で title を抽出する relative selector (空 = link text)
        state_file: seen URL を保存する JSON ファイルパス
        feed_title: Article.feed_title に入る表示用名前
        max_posts_per_run: 1 fetch あたりの上限
        url_include_pattern: 抽出後 URL を更に絞る regex (空 = 無効)
        url_exclude_pattern: 除外 regex (空 = 無効)
    """

    name: str
    listing_url: str
    article_link_selector: str
    state_file: Path
    feed_title: str
    title_selector: str = ""
    max_posts_per_run: int = DEFAULT_MAX_POSTS_PER_RUN
    url_include_pattern: str = ""
    url_exclude_pattern: str = ""
    # 収集 href の決定論的書換 (regex → replacement、先頭 1 回)。BSI 型の
    # 「listing が検索フォーム deeplink (404) を返し canonical は prefix 除去」への対処。
    url_rewrite_pattern: str = ""
    url_rewrite_replacement: str = ""
    user_agent: str = DEFAULT_USER_AGENT
    http_timeout: float = DEFAULT_HTTP_TIMEOUT
    # 死活観測 (2026-08-02)。frozen dataclass だが中身は可変ホルダなので記録できる。
    # 「新着 0 件」と「取得が壊れた」を区別する唯一の手段 — 成果ではなく行為を残す。
    observation: FetchObservation = field(
        default_factory=FetchObservation, compare=False, repr=False
    )

    def last_fetch_outcome(self) -> SourceFetchOutcome | None:
        """直近取得の死活 (未取得なら None)。結合キーは購読一覧と同じ listing URL。"""
        return self.observation.to_outcome(self.listing_url, self.feed_title or self.name)

    # ---- HTML fetch + selector apply ----

    async def _fetch_listing(self) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=self.http_timeout,
                follow_redirects=True,
            ) as client:
                # fetch_policy: bot UA → WAF ブロック時のみブラウザ UA (プレビューと同一)
                resp, _stage = await staged_get(
                    client, self.listing_url, identity=self.user_agent, accept=HTML_ACCEPT
                )
        except httpx.HTTPError as e:
            _log.warning("html_listing_fetch_failed", watcher=self.name, error=str(e))
            return ""
        if resp.status_code < 400:
            return resp.text or ""
        # 第3段 (監査 2026-08-01 追補): UA 2 段でも解けない JS チャレンジは Playwright で
        # 突破 (本文取得層と同じ最終段を listing 層にも)。発火はブロック署名 + 指紋のみ。
        from src.tools.fetch_policy import looks_like_js_challenge, should_escalate
        from src.tools.playwright_fetch import fetch_text_via_playwright

        if should_escalate(resp.status_code) and looks_like_js_challenge(resp.text or ""):
            pw_text = await fetch_text_via_playwright(self.listing_url, user_agent=self.user_agent)
            if pw_text:
                _log.info("html_listing_playwright_recovered", watcher=self.name)
                return pw_text
        _log.warning(
            "html_listing_fetch_failed",
            watcher=self.name,
            error=f"HTTP {resp.status_code}",
        )
        return ""

    def _apply_selectors(self, html: str) -> list[tuple[str, str]]:
        """selectors apply → (url, title) tuple list を返す。"""
        try:
            from bs4 import BeautifulSoup
        except ImportError as e:
            _log.error("html_listing_bs4_missing", watcher=self.name, error=str(e))
            return []
        soup = BeautifulSoup(html, "html.parser")
        out: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        try:
            elements = soup.select(self.article_link_selector)
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "html_listing_selector_invalid",
                watcher=self.name,
                selector=self.article_link_selector,
                error=str(e),
            )
            return []
        for el in elements:
            href = el.get("href")
            if not href:
                inner = el.find("a") if hasattr(el, "find") else None
                if inner is not None:
                    href = inner.get("href")
            if not href:
                continue
            full_url = urljoin(self.listing_url, str(href))
            if self.url_rewrite_pattern:
                import re as _re

                try:
                    full_url = _re.sub(
                        self.url_rewrite_pattern,
                        self.url_rewrite_replacement,
                        full_url,
                        count=1,
                    )
                except _re.error as e:
                    _log.warning("html_listing_rewrite_invalid", watcher=self.name, error=str(e))
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            # title 抽出
            title = ""
            if self.title_selector:
                try:
                    t_el = el.select_one(self.title_selector)
                    if t_el is not None:
                        title = t_el.get_text(" ", strip=True)
                except Exception:  # noqa: BLE001
                    title = ""
            if not title:
                title = el.get_text(" ", strip=True)
            if len(title) > 200:
                title = title[:200]
            out.append((full_url, title or full_url))
        return out

    def _filter_urls(self, items: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """include / exclude regex で再 filter (selector 漏れを後追い除外)。"""
        if not self.url_include_pattern and not self.url_exclude_pattern:
            return items
        import re

        inc = re.compile(self.url_include_pattern) if self.url_include_pattern else None
        exc = re.compile(self.url_exclude_pattern) if self.url_exclude_pattern else None
        out: list[tuple[str, str]] = []
        for url, title in items:
            if inc is not None and not inc.search(url):
                continue
            if exc is not None and exc.search(url):
                continue
            out.append((url, title))
        return out

    # ---- seen state (SitemapWatcher と同 schema) ----

    def _load_seen(self) -> set[str]:
        if not self.state_file.exists():
            return set()
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return set(data.get("seen_urls", []))
        except (json.JSONDecodeError, OSError) as e:
            _log.warning("seen_load_failed", watcher=self.name, error=str(e))
            return set()

    def _save_seen(self, urls: set[str]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"seen_urls": sorted(urls)}
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_file)

    # ---- Article 変換 ----

    def _to_article(self, url: str, title: str) -> Article:
        article_id = f"{self.name}:{hashlib.sha1(url.encode()).hexdigest()[:16]}"
        return Article(
            id=article_id,
            title=title[:200] if title else url[:200],
            url=url,
            summary_html="",
            author=None,
            published=datetime.now().astimezone(),
            feed_title=self.feed_title,
            feed_url=self.listing_url,
        )

    # ---- 公開 API ----

    async def fetch_articles(self, max_count: int | None = None) -> list[Article]:
        cap = min(max_count or self.max_posts_per_run, self.max_posts_per_run)

        html = await self._fetch_listing()
        if not html:
            self.observation.record_failure("listing ページを取得できませんでした")
            return []
        items = self._apply_selectors(html)
        if not items:
            # listing は 200 で返っているのに抽出 0 件 = selector 陳腐化 (サイト改修) の
            # 典型。ここを ok 扱いにすると「低頻度で静か」と見分けがつかず無音で死ぬ。
            _log.warning(
                "html_listing_no_matches",
                watcher=self.name,
                selector=self.article_link_selector,
            )
            self.observation.record_failure(
                "listing は取得できましたが記事リンクを抽出できません "
                "(セレクタがサイト構造の変更で機能していない可能性)"
            )
            return []
        items = self._filter_urls(items)
        if not items:
            self.observation.record_failure(
                "記事リンクは抽出できましたが URL フィルタに一致しません "
                "(include/exclude パターンとサイト構造の不一致)"
            )
            return []
        self.observation.record_success(len(items))
        seen = self._load_seen()
        new_items = [(u, t) for u, t in items if u not in seen]

        _log.info(
            "html_listing_fetch",
            watcher=self.name,
            total=len(items),
            seen=len(seen),
            new=len(new_items),
        )
        if not new_items:
            return []
        if len(new_items) > cap:
            new_items = new_items[:cap]

        seen.update(u for u, _ in new_items)
        self._save_seen(seen)

        return [self._to_article(u, t) for u, t in new_items]

    async def init_seen(self) -> int:
        html = await self._fetch_listing()
        items = self._apply_selectors(html)
        items = self._filter_urls(items)
        urls = {u for u, _ in items}
        self._save_seen(urls)
        _log.info("html_listing_init", watcher=self.name, marked_seen=len(urls))
        return len(urls)

    def read_state(self) -> WatcherState:
        if not self.state_file.exists():
            return WatcherState(
                name=self.name,
                seen_count=0,
                state_file_exists=False,
                last_modified=None,
                state_path=str(self.state_file),
            )
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            seen_count = len(data.get("seen_urls", []))
        except (json.JSONDecodeError, OSError) as e:
            _log.warning("seen_state_read_failed", watcher=self.name, error=str(e))
            seen_count = 0
        mtime = datetime.fromtimestamp(self.state_file.stat().st_mtime).astimezone()
        return WatcherState(
            name=self.name,
            seen_count=seen_count,
            state_file_exists=True,
            last_modified=mtime,
            state_path=str(self.state_file),
        )
