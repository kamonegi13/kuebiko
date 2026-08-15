"""Direct RSS / Atom feed fetcher (Phase X-1: Inoreader 廃止に向けた parallel run)。

Inoreader API を介さず HTTP で直接 RSS / Atom feed を fetch、Article に変換する。
Inoreader と並列で動かして coverage 比較した後、最終的に Inoreader 経路を廃止する
ロードマップの第 1 段階。

設計方針:
- ``ArticleSource`` Protocol 実装、既存 pipeline にそのまま挿入可能
- ``config/sources/feeds.yaml`` で feed list を宣言、site 固有 hardcode なし
- feedparser library で RSS 1.0 / 2.0 / Atom 1.0 を統一処理
- per-feed エラーは drop + log (1 feed 失敗で全体停止しない)
- async httpx で 並列 fetch、feedparser parse は to_thread で sync→async
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from collections.abc import Callable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import feedparser  # type: ignore[import-untyped]
import httpx
from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger
from src.sources import source_store
from src.tools.article_model import Article
from src.tools.fetch_policy import FEED_ACCEPT, staged_get
from src.tools.source_fetch_outcome import SourceFetchOutcome
from src.tools.url_normalizer import url_hash

_log = get_logger(__name__)

DEFAULT_FEEDS_YAML = Path("config/sources/feeds.yaml")
DEFAULT_USER_AGENT = "kuebiko/1.0 (+https://github.com/kamonegi13/kuebiko)"
DEFAULT_HTTP_TIMEOUT = 30.0
DEFAULT_PER_FEED_LIMIT = 50
DEFAULT_CONCURRENT_FEEDS = 8
# 未見でもこれより古い published は返さない。新規 feed 追加時の過去記事流入と、
# dedup 記憶 purge 後の恒常掲載 URL 再出現を防ぐ (P0 収集飢餓修正の安全弁)。
DEFAULT_UNSEEN_MAX_AGE_DAYS = 14

# seen 済み url_hash の集合を返すバルク照合 (P0: 未見選別を切り詰めの前に行うための DI。
# tools/ 層は DB 非依存を保ち、呼び出し側が dedup repo のメソッドを注入する)。
SeenHashFilter = Callable[[Sequence[str]], AbstractSet[str]]


class FeedDef(BaseModel):
    """1 feed の定義。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    url: str
    enabled: bool = True
    folder: str = ""


class FeedsConfig(BaseModel):
    """``config/sources/feeds.yaml`` の root schema。"""

    model_config = ConfigDict(extra="forbid")

    feeds: list[FeedDef] = Field(default_factory=list)


def load_feeds_config(path: Path | None = None) -> FeedsConfig:
    """feed list を load。

    runtime SSoT は config_store (DB)。yaml は seed 専用。``path`` 明示時のみ yaml を
    直読みする (テスト / seed)。flag 分岐の詳細は ``src/sources/source_store.py``。
    """
    entries = source_store.load_entries("rss", path=path)
    return FeedsConfig.model_validate({"feeds": entries})


@dataclass(frozen=True)
class FetchResult:
    """1 feed の fetch 結果 (debug 用)。"""

    feed: FeedDef
    articles: list[Article]
    error: str | None = None
    # どの段で取得できたか (fetch_policy)。"browser" = bot UA がブロックされ
    # エスカレーションで取得した feed (WAF 側の方針変化の観測点)
    stage: str = "bot"


def _entry_to_article(entry: dict[str, Any], feed: FeedDef) -> Article | None:
    """feedparser entry を Article に変換。必須 field 欠落なら None。"""
    # link は title-only entry の場合 missing なので skip
    link = entry.get("link") or ""
    if not link:
        return None
    title = (entry.get("title") or "").strip() or "(no title)"

    # 本文: summary > content > description
    summary_html = ""
    if "summary" in entry:
        summary_html = entry["summary"] or ""
    elif "content" in entry and entry["content"]:
        summary_html = entry["content"][0].get("value", "") or ""
    elif "description" in entry:
        summary_html = entry["description"] or ""

    # 公開日時: published_parsed > updated_parsed > now
    published: datetime
    if entry.get("published_parsed"):
        from time import struct_time

        st: struct_time = entry["published_parsed"]
        published = datetime(*st[:6], tzinfo=UTC)
    elif entry.get("updated_parsed"):
        st = entry["updated_parsed"]
        published = datetime(*st[:6], tzinfo=UTC)
    else:
        published = datetime.now(UTC)

    # 著者
    author = entry.get("author") or None

    # ID: entry.id があれば使う、なければ URL の hash
    if entry.get("id"):
        article_id = f"rss:{entry['id']}"
    else:
        url_hash = hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]  # noqa: S324
        article_id = f"rss:{url_hash}"

    return Article(
        id=article_id,
        title=title,
        url=link,
        summary_html=summary_html,
        author=author,
        published=published,
        feed_title=feed.name,
        feed_url=feed.url,
    )


async def _fetch_one_feed(
    client: httpx.AsyncClient,
    feed: FeedDef,
    per_feed_limit: int = DEFAULT_PER_FEED_LIMIT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> FetchResult:
    """1 feed を fetch + parse、Article list を返す。

    取得は fetch_policy の段階的エスカレーション (bot UA → ブロック時のみブラウザ UA)。
    プレビュー経路と同じポリシーを共有する (2026-08-01: 経路分岐でプレビューだけ
    成功し本番が 403 で連続失敗する事故の根治)。
    """
    stage = "bot"
    try:
        r, stage = await staged_get(client, feed.url, identity=user_agent, accept=FEED_ACCEPT)
        r.raise_for_status()
    except httpx.HTTPError as e:
        _log.warning("rss_feed_http_error", feed=feed.name, url=feed.url, error=str(e))
        return FetchResult(feed=feed, articles=[], error=f"http: {e}", stage=stage)

    # feedparser は sync なので to_thread で wrap
    try:
        parsed = await asyncio.to_thread(feedparser.parse, r.content)
    except Exception as e:  # noqa: BLE001
        _log.warning("rss_feed_parse_error", feed=feed.name, error=str(e))
        return FetchResult(feed=feed, articles=[], error=f"parse: {e}", stage=stage)

    # feedparser bozo flag: 構造的に不正な feed
    if parsed.get("bozo") and not parsed.entries:
        bozo_exc = parsed.get("bozo_exception", "")
        _log.warning("rss_feed_bozo_no_entries", feed=feed.name, error=str(bozo_exc))
        return FetchResult(feed=feed, articles=[], error=f"bozo: {bozo_exc}", stage=stage)

    articles: list[Article] = []
    for entry in parsed.entries[:per_feed_limit]:
        article = _entry_to_article(entry, feed)
        if article is not None:
            articles.append(article)

    _log.info(
        "rss_feed_fetched",
        feed=feed.name,
        url=feed.url,
        entry_count=len(parsed.entries),
        article_count=len(articles),
        stage=stage,
    )
    return FetchResult(feed=feed, articles=articles, stage=stage)


class DirectRssSource:
    """``ArticleSource`` Protocol 実装、複数 RSS / Atom feed を直接 fetch。"""

    def __init__(
        self,
        feeds: list[FeedDef],
        *,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        per_feed_limit: int = DEFAULT_PER_FEED_LIMIT,
        concurrent_feeds: int = DEFAULT_CONCURRENT_FEEDS,
        user_agent: str = DEFAULT_USER_AGENT,
        seen_hash_filter: SeenHashFilter | None = None,
        unseen_max_age_days: int = DEFAULT_UNSEEN_MAX_AGE_DAYS,
    ) -> None:
        self._feeds = [f for f in feeds if f.enabled]
        self._timeout = http_timeout
        self._per_feed_limit = per_feed_limit
        self._concurrent = concurrent_feeds
        self._user_agent = user_agent
        self._seen_filter = seen_hash_filter
        self._unseen_max_age = timedelta(days=unseen_max_age_days)
        # 直近 fetch の per-feed 結果 (feed 死活検知が読む。fetch 前は空)
        self.last_results: tuple[FetchResult, ...] = ()

    async def fetch(self, max_count: int) -> list[Article]:
        """全 feed を並列 fetch、**未見記事のみ**が max_count を競う (P0 収集飢餓修正)。

        旧実装は全 feed 混合 pool を published 降順 top-N に切ってから下流で dedup して
        いたため、pool の ~97% を占める既知記事との recency 競争で、低頻度/バッチ日付
        feed (CISA advisories 等) の未見記事が構造的に飢餓した。ここでは
        seen 済み URL の除外 → 鮮度ガード → per-feed 公平枠 の順で選抜する。
        ``seen_hash_filter`` 未指定時 (テスト/独立利用) は旧挙動。
        """
        if not self._feeds:
            _log.info("rss_source_no_feeds")
            return []

        semaphore = asyncio.Semaphore(self._concurrent)
        # ヘッダは fetch_policy が per-request で組む (Accept の */* フォールバック含む)。
        # bot UA がブロックされた feed だけブラウザ UA へエスカレーションする
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:

            async def _bounded_fetch(feed: FeedDef) -> FetchResult:
                async with semaphore:
                    return await _fetch_one_feed(
                        client, feed, self._per_feed_limit, user_agent=self._user_agent
                    )

            results = await asyncio.gather(*[_bounded_fetch(f) for f in self._feeds])

        all_articles: list[Article] = []
        error_count = 0
        for r in results:
            if r.error:
                error_count += 1
            all_articles.extend(r.articles)
        self.last_results = tuple(results)

        if self._seen_filter is None:
            # 旧挙動 (テスト/独立利用): published 降順 top-N
            all_articles.sort(key=lambda a: a.published, reverse=True)
            truncated = all_articles[:max_count]
            _log.info(
                "rss_source_fetch_complete",
                feeds_total=len(self._feeds),
                feeds_failed=error_count,
                articles_total=len(all_articles),
                articles_returned=len(truncated),
                max_count=max_count,
            )
            return truncated

        selected, stats = self._select_unseen_fair(all_articles, max_count)
        _log.info(
            "rss_source_fetch_complete",
            feeds_total=len(self._feeds),
            feeds_failed=error_count,
            articles_total=len(all_articles),
            articles_returned=len(selected),
            max_count=max_count,
            **stats,
        )
        return selected

    def last_fetch_health(self) -> list[SourceFetchOutcome]:
        """直近 fetch の per-feed 死活 (transport 横断 seam、2026-08-02)。

        RSS の「行為の成立」は取得 + parse まで — entry 0 件は「ソースが 0 件提示した」
        という観測であって失敗ではない (feedparser は汎用パーサなので、自前 selector を
        使う html_scraper/sitemap とは境界が違う。src/tools/source_fetch_outcome.py 参照)。
        """
        return [
            SourceFetchOutcome(
                source_key=r.feed.url,
                name=r.feed.name,
                ok=r.error is None,
                item_count=len(r.articles),
                error=r.error or "",
            )
            for r in self.last_results
        ]

    def _select_unseen_fair(
        self, pool: list[Article], max_count: int
    ) -> tuple[list[Article], dict[str, int]]:
        """未見選別 → 鮮度ガード → per-feed 公平枠 (round-robin) の決定論選抜。

        公平枠: 未見数が max_count を超えるキャッチアップ時も、feed ごとに新しい順で
        1 件ずつ round-robin 採用する。単一の高頻度 feed が枠を独占して他 feed を
        飢餓させない (バースト時も全 feed に代表権)。選外の未見記事は seen 登録されない
        ため次 run で再候補になる (恒久ロストしない)。
        """
        assert self._seen_filter is not None
        hashes = [url_hash(a.url) for a in pool]
        seen = self._seen_filter(hashes) if hashes else frozenset()
        cutoff = datetime.now(UTC) - self._unseen_max_age

        batch: set[str] = set()
        fresh_unseen: list[Article] = []
        age_excluded = 0
        for article, h in zip(pool, hashes, strict=True):
            if h in seen or h in batch:
                continue
            batch.add(h)
            published = (
                article.published
                if article.published.tzinfo
                else article.published.replace(tzinfo=UTC)
            )
            if published < cutoff:
                age_excluded += 1
                continue
            fresh_unseen.append(article)

        by_feed: dict[str, list[Article]] = {}
        for article in fresh_unseen:
            by_feed.setdefault(article.feed_title, []).append(article)
        for lst in by_feed.values():
            lst.sort(key=lambda a: a.published, reverse=True)

        selected: list[Article] = []
        queues = deque(sorted(by_feed.items()))  # feed 名順 = 決定論
        while queues and len(selected) < max_count:
            name, lst = queues.popleft()
            selected.append(lst.pop(0))
            if lst:
                queues.append((name, lst))
        selected.sort(key=lambda a: a.published, reverse=True)

        stats = {
            "pool_seen": len(seen & set(hashes)),
            "pool_unseen_fresh": len(fresh_unseen),
            "age_excluded": age_excluded,
            "feeds_with_unseen": len(by_feed),
        }
        return selected, stats


__all__: list[str] = [
    "DirectRssSource",
    "FeedDef",
    "FeedsConfig",
    "FetchResult",
    "load_feeds_config",
]
