"""複数情報源 (RSS / Grok email + Playwright / web scraper) を統一する Source 層 (Phase 2)。

Phase 1 は RSS 単独だったが、Phase 2 から Grok 通知メールが入ったため、
``run_pipeline`` が複数の取得経路を扱えるよう Article への正規化層を新設する。

設計方針:
- 出力型は ``Article`` (src.tools.article_model.Article) を共通使用
- 各 Source は ``async def fetch(...) -> list[Article]`` を実装する
- ``run_pipeline`` 側は Source 種別を意識しない (依存性逆転)
- Source 失敗時は他の Source の動作を妨げない (個別 try/except)
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import trafilatura

from src.config_loader import AppConfig, SourceConfig
from src.grok.fetcher import GrokFetcher
from src.logging_config import get_logger
from src.tools.article_model import Article
from src.tools.content_extractor import ContentExtractor
from src.tools.imap_client import EmailMessage, ImapClient
from src.tools.source_fetch_outcome import SourceFetchOutcome

# テスト容易化のためモジュール参照を明示
_trafilatura_extract = trafilatura.extract

_log = get_logger(__name__)


class ArticleSource(Protocol):
    """すべての情報源が満たすべきインタフェース。"""

    async def fetch(self, max_count: int) -> list[Article]:
        """``max_count`` 件以下の記事を取得する。失敗時は例外を投げて良い。"""
        ...


# ---------- Grok Email Source ----------


class GrokEmailSource:
    """Gmail で Grok 通知メールを受信 → Playwright で本文取得 → Article に変換。"""

    # Grok メールの host (grok.com) 配下で **CTI レポートではない** path。
    # task-unsubscribe / login 等を抽出対象から除外する。
    _NON_REPORT_PATH_PREFIXES: tuple[str, ...] = (
        "/http/task-unsubscribe",
        "/http/unsubscribe",
        "/oauth",
        "/login",
        "/sign-in",
        "/auth",
        "/api/",
        "/static/",
    )

    def __init__(
        self,
        imap_client: ImapClient,
        grok_fetcher: GrokFetcher,
        extractor: ContentExtractor,
        *,
        sender_filters: list[str] | None = None,
        subject_filters: list[str] | None = None,
        lookback_minutes: int | None = None,
        mark_as_read: bool = False,
        unseen_only: bool = True,
        url_host_allowlist: list[str] | None = None,
    ) -> None:
        self._imap = imap_client
        self._grok = grok_fetcher
        self._extractor = extractor
        self._sender_filters = sender_filters or []
        self._subject_filters = subject_filters or []
        self._lookback_minutes = lookback_minutes
        self._mark_as_read = mark_as_read
        self._unseen_only = unseen_only
        # メール本文には HTML の DOCTYPE / namespace 宣言など boilerplate URL も
        # 混入するため、Grok レポートのドメインに絞り込む (default: grok.com)
        self._url_host_allowlist = url_host_allowlist or ["grok.com"]
        # 監査残項目① (2026-07-05): セッション失効の検出数。run_pipeline が errors に
        # 搬送して可視失敗にする (従来は info ログのみ = 「🟢 取得 0」に化けていた)
        self.last_session_expired_count: int = 0

    async def fetch(self, max_count: int) -> list[Article]:
        # mark_as_read=False で取得 (BODY.PEEK)。既読化は **レポート抽出に成功した
        # メールのみ** 後段で明示的に行う (失敗メールは未読に残して再試行可能にする)。
        self.last_session_expired_count = 0
        emails = await self._imap.fetch_unread(
            sender_filters=self._sender_filters,
            subject_filters=self._subject_filters,
            lookback_minutes=self._lookback_minutes,
            max_messages=max_count * 2,  # 1 メールに複数 URL があり得るので倍取得
            mark_as_read=False,
            unseen_only=self._unseen_only,
        )
        if not emails:
            _log.info("grok_email_source_no_messages")
            return []

        articles: list[Article] = []
        processed_uids: list[str] = []  # レポート抽出に成功したメール (既読化対象)
        for msg in emails:
            candidates = [
                u
                for u in msg.extracted_urls
                if self._url_host_allowed(u) and not self._is_non_report_url(u)
            ]
            if not candidates:
                _log.info(
                    "grok_email_no_matching_url",
                    email_uid=msg.uid,
                    total_urls=len(msg.extracted_urls),
                    allowlist=self._url_host_allowlist,
                )
                continue
            got_article = False
            for url in candidates:
                if len(articles) >= max_count:
                    break
                try:
                    article = await self._fetch_one(url, msg)
                except Exception as e:  # noqa: BLE001
                    _log.warning(
                        "grok_email_fetch_failed",
                        url=url,
                        email_uid=msg.uid,
                        error=str(e),
                    )
                    continue
                if article is not None:
                    articles.append(article)
                    got_article = True
            if got_article:
                processed_uids.append(msg.uid)
            if len(articles) >= max_count:
                break

        # Grok レポートを読み取れたメールのみ既読化 (インボックス肥大防止 + 失敗は再試行)
        marked = 0
        if self._mark_as_read and processed_uids:
            marked = await self._imap.mark_seen(processed_uids)

        _log.info(
            "grok_email_source_completed",
            email_count=len(emails),
            article_count=len(articles),
            marked_seen=marked,
        )
        return articles

    def _url_host_allowed(self, url: str) -> bool:
        """URL の host が allowlist にあるかを判定 (サブドメイン許容)。"""
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return False
        if not host:
            return False
        return any(
            host == allowed.lower() or host.endswith("." + allowed.lower())
            for allowed in self._url_host_allowlist
        )

    def _is_non_report_url(self, url: str) -> bool:
        """grok.com の同一ドメインでも、CTI レポート以外のページ (購読解除・認証等)
        を URL path で除外する。これらを抽出対象にすると LLM/renderer が無意味な
        コンテンツを処理してしまう。
        """
        try:
            path = urlparse(url).path or ""
        except ValueError:
            return False
        path_lower = path.lower()
        return any(path_lower.startswith(p) for p in self._NON_REPORT_PATH_PREFIXES)

    async def _fetch_one(self, url: str, msg: EmailMessage) -> Article | None:
        result = await self._grok.fetch(url)
        if not result.success:
            if result.failure_reason == "session_expired":
                # 認証失効はレポート恒久ロストに直結する (lookback 窓を抜けると
                # 再取得不能)。warning + カウントで run_pipeline が errors に載せる。
                self.last_session_expired_count += 1
                _log.warning("grok_session_expired_detected", url=url)
            else:
                _log.info(
                    "grok_fetch_skipped",
                    url=url,
                    reason=result.failure_reason,
                )
            return None

        # 本文ソースの優先順 (Grok チャットページは Next.js SPA で trafilatura
        # が抽出できないため、Playwright 側で抽出した DOM テキストを優先する):
        #   1. result.body_text (DOM から抽出した hydration 済み本文)
        #   2. trafilatura で HTML から本文化
        #   3. メールの summary_html (フォールバック)
        body = result.body_text.strip() if result.body_text else ""
        if not body:
            from src.tools import source_router as _self_module

            extracted = _self_module._trafilatura_extract(
                result.html,
                include_comments=False,
                deduplicate=False,
            )
            body = (extracted or "").strip()

        if not body:
            return None

        # body を summary_html に積み込む (Article は HTML 想定だが、
        # 後続の _resolve_body / _strip_html はプレーンテキストでも動作する)
        summary_payload = body if body else (msg.body_html or msg.body_text)

        return Article(
            id=_stable_article_id(url, msg.uid),
            title=result.title or msg.subject or url,
            url=result.final_url,
            summary_html=summary_payload,
            author=msg.sender or None,
            published=msg.received_at,
            feed_title="Grok",
            feed_url="https://grok.com/",
        )


# ---------- helpers ----------


def _stable_article_id(url: str, fallback: str) -> str:
    """URL ベースで安定した ID を生成する (重複排除に役立つ)。"""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]  # noqa: S324
    return f"grok:{digest}:{fallback}"


# ---------- Web Scraper Sources (Phase 5R / 5T-B / 5T-C) ----------


# 動的 import で循環依存を回避。新 watcher 追加時はここに entry を追加するだけ。
def _resolve_scraper(name: str) -> Any:
    """scraper name → async fetch_articles 関数を解決。

    Phase X-2 で yaml registry を優先するように変更。
    yaml に未登録の bespoke watcher (north_38 / nicter 等の
    custom HTML/Playwright 系) は旧 import を維持。
    """
    # Phase X-2: yaml registry で sitemap 系 watcher を解決
    from src.watchers.yaml_registry import get_registry

    yaml_registry = get_registry()
    if name in yaml_registry:
        watcher = yaml_registry[name]

        async def _yaml_fetch(max_count: int = 10) -> list[Article]:
            return await watcher.fetch_articles(max_count=max_count)

        return _yaml_fetch

    # Phase E-3: scrapers registry で HTML scraper を解決
    from src.watchers.scrapers_registry import get_registry as get_scrapers_registry

    scrapers_registry = get_scrapers_registry()
    if name in scrapers_registry:
        scraper = scrapers_registry[name]

        async def _scraper_fetch(max_count: int = 8) -> list[Article]:
            return await scraper.fetch_articles(max_count=max_count)

        return _scraper_fetch

    # yaml 未登録 = custom Playwright/bespoke 系 (宣言的 yaml で表現できないもののみ)
    if name == "38north":
        from src.watchers.north_38 import fetch_articles
    elif name == "nicter":
        from src.watchers.nicter import fetch_articles
    else:
        raise ValueError(f"未知の scraper_name: {name}")
    return fetch_articles


class WebScraperSource:
    """HTML scraping ベースの source (Project Zero / LAC Watch 等)。

    Phase 5R で導入: 元々独立 watcher として Discord 直接 post していた
    Project Zero / LAC Watch を主パイプラインに合流させるためのアダプタ。

    各 scraper モジュール (``src/watchers/<name>.py``) は ``fetch_articles()``
    を export しており、ここではそれを呼んで Article リストを返すだけ。
    LLM 要約 / triage / dedup / channel routing は主パイプラインに委譲する。
    """

    def __init__(self, scraper_name: str) -> None:
        self._scraper_name = scraper_name
        self._fetch_articles = _resolve_scraper(scraper_name)

    async def fetch(self, max_count: int) -> list[Article]:
        articles: list[Article] = await self._fetch_articles(max_count=max_count)
        _log.info(
            "web_scraper_source_fetched",
            scraper=self._scraper_name,
            count=len(articles),
        )
        return articles


class WebScraperClusterSource:
    """複数の web_scraper を 1 pipeline で並列実行する集約 source (Phase 5T-C / 5T-D)。

    Phase 5T-B で watcher が増え yaml が冗長化したのを 1 cluster に集約。
    Phase 5T-D で個別 watcher の ``enabled`` フラグに対応 (yaml 編集で切替可能)。

    動作:
        - ``scrapers`` のうち ``enabled=True`` のものを ``asyncio.gather`` で並列 fetch
        - 全 article を 1 つの list に結合
        - ``max_articles_per_scraper`` で per-watcher 上限、``max_count`` で総数上限
        - 個別 watcher 失敗は warning ログのみで全体は継続

    yaml 設定例:
        source:
          type: web_scraper_cluster
          scrapers:
            - {name: project-zero, enabled: true}
            - {name: ccdcoe, enabled: false}  # 個別 off
          max_articles: 80
          max_articles_per_scraper: 8
    """

    def __init__(
        self,
        scrapers: list[tuple[str, bool]],
        max_articles_per_scraper: int = 8,
    ) -> None:
        # scrapers: [(name, enabled), ...] のタプルリスト
        self._all_scrapers = list(scrapers)
        # enabled のみ fetcher を resolve (disabled は import せず skip)
        self._active_names = [name for name, enabled in scrapers if enabled]
        self._fetchers = [_resolve_scraper(name) for name in self._active_names]
        self._per_max = max_articles_per_scraper

    @property
    def all_scrapers(self) -> list[tuple[str, bool]]:
        """UI 表示用に全 scraper (disabled 含む) の (name, enabled) を返す。"""
        return list(self._all_scrapers)

    async def fetch(self, max_count: int) -> list[Article]:
        import asyncio

        async def _one(name: str, fetcher: Any) -> list[Article]:
            try:
                result: list[Article] = await fetcher(max_count=self._per_max)
                return result
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "web_scraper_cluster_one_failed",
                    scraper=name,
                    error=str(e),
                )
                return []

        if not self._active_names:
            _log.warning("web_scraper_cluster_no_active_scrapers")
            return []

        results = await asyncio.gather(
            *(
                _one(name, fetcher)
                for name, fetcher in zip(self._active_names, self._fetchers, strict=False)
            ),
        )
        flat = [a for sublist in results for a in sublist]
        _log.info(
            "web_scraper_cluster_fetched",
            active_scrapers=self._active_names,
            disabled_count=len(self._all_scrapers) - len(self._active_names),
            per_scraper_counts=[len(r) for r in results],
            total=len(flat),
        )
        return flat[:max_count]

    def last_fetch_health(self) -> list[SourceFetchOutcome]:
        """直近 fetch の per-scraper 死活 (RSS の ``last_results`` に対応する観測点)。

        **成果 (新着記事数) ではなく行為 (listing/sitemap を取得し項目を抽出できたか)**
        を返す — 低頻度で静かなソースと、selector が腐って無音で死んだソースを
        区別できるようにするため (2026-08-02)。registry は instance を cache するため
        cluster が走らせた watcher と同一 instance から観測を取り出せる。
        bespoke scraper (nicter 等の .py) は観測を持たないので対象外。
        """
        from src.watchers.scrapers_registry import get_registry as get_scrapers_registry
        from src.watchers.yaml_registry import get_registry as get_yaml_registry

        out: list[SourceFetchOutcome] = []
        for name in self._active_names:
            watcher = get_yaml_registry().get(name) or get_scrapers_registry().get(name)
            if watcher is None:
                continue
            outcome = watcher.last_fetch_outcome()
            if outcome is not None:
                out.append(outcome)
        return out


# ---------- Factory ----------


def build_source(
    source_config: SourceConfig,
    *,
    extractor: ContentExtractor | None = None,
    grok_fetcher: GrokFetcher | None = None,
    imap_client: ImapClient | None = None,
    app_config: AppConfig | None = None,
    seen_hash_filter: Callable[[Sequence[str]], AbstractSet[str]] | None = None,
) -> ArticleSource:
    """``SourceConfig.type`` に対応する ArticleSource インスタンスを返す。

    呼び出し側は必要な依存を渡すだけで、Source 種別固有の生成ロジックを意識しない。
    """
    stype = source_config.type
    if stype == "grok_email":
        missing: list[str] = []
        if imap_client is None:
            missing.append("imap_client")
        if grok_fetcher is None:
            missing.append("grok_fetcher")
        if extractor is None:
            missing.append("extractor")
        if missing:
            raise ValueError(
                f"grok_email source は次の依存が必要: {', '.join(missing)}",
            )
        assert imap_client is not None and grok_fetcher is not None and extractor is not None
        return GrokEmailSource(
            imap_client=imap_client,
            grok_fetcher=grok_fetcher,
            extractor=extractor,
            sender_filters=source_config.grok_sender_filters,
            subject_filters=source_config.grok_subject_filters,
            lookback_minutes=source_config.grok_lookback_minutes,
            mark_as_read=source_config.grok_mark_as_read,
            unseen_only=source_config.grok_unseen_only,
            url_host_allowlist=source_config.grok_url_host_allowlist,
        )
    if stype == "rss":
        # 直接 RSS fetcher (config/sources/feeds.yaml 駆動)。
        # ``config/sources/feeds.yaml`` から feed list を load して並列 fetch する。
        from src.tools.direct_rss_source import (
            DEFAULT_UNSEEN_MAX_AGE_DAYS,
            DirectRssSource,
            load_feeds_config,
        )

        feeds_path = source_config.feeds_config_path
        cfg = load_feeds_config(Path(feeds_path)) if feeds_path else load_feeds_config()
        return DirectRssSource(
            feeds=cfg.feeds,
            http_timeout=source_config.rss_http_timeout or 30.0,
            per_feed_limit=source_config.rss_per_feed_limit or 50,
            seen_hash_filter=seen_hash_filter,
            unseen_max_age_days=(
                source_config.rss_unseen_max_age_days or DEFAULT_UNSEEN_MAX_AGE_DAYS
            ),
        )
    if stype == "web_scraper":
        # Phase 5R: HTML scraping ベースの source
        scraper_name = source_config.scraper_name
        if not scraper_name:
            raise ValueError("web_scraper source は scraper_name が必要です")
        return WebScraperSource(scraper_name=scraper_name)
    if stype == "web_scraper_cluster":
        # Phase 5T-C/D + 統合再設計: yaml 定義ソース (scrapers.yaml/watchers.yaml) は
        # registry から enabled な全件を auto-collect (enabled は entry.yaml が単一 SSoT)。
        # pipelines.yaml の source.scrapers には yaml に持ち場の無い custom .py scraper
        # (38north / nicter 等の Playwright/bespoke 系) のみを明示列挙する。
        from src.watchers.scrapers_registry import get_registry as _scr_registry
        from src.watchers.scrapers_registry import load_scrapers_config
        from src.watchers.yaml_registry import get_registry as _wat_registry
        from src.watchers.yaml_registry import load_watchers_config

        all_yaml_names = {s.name for s in load_scrapers_config().scrapers} | {
            w.name for w in load_watchers_config().watchers
        }
        # registry は enabled な entry のみ返す = entry.enabled が SSoT
        auto_enabled = set(_scr_registry().keys()) | set(_wat_registry().keys())
        # 明示リストのうち yaml に無い custom のみ採用 (yaml 名は registry が決定権)
        customs = [
            (e.name, e.enabled) for e in source_config.scrapers if e.name not in all_yaml_names
        ]
        merged = [(n, True) for n in sorted(auto_enabled)] + customs
        if not merged:
            raise ValueError("web_scraper_cluster source に有効な scraper がありません")
        return WebScraperClusterSource(
            scrapers=merged,
            max_articles_per_scraper=source_config.max_articles_per_scraper,
        )
    raise ValueError(f"未知の source type: {stype}")


# ---------- 補助関数: 現在時刻 (テスト用に注入可能) ----------


def utcnow() -> datetime:
    return datetime.now(UTC)
