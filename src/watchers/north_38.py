"""38 North watcher (Phase 5T-F-2)。

North Korea 専門分析機関 (Stimson Center 配下)。DPRK 核・ミサイル開発の
標準分析ソース。Cloudflare strict bot protection 配下で httpx 直接取得は
失敗するため、Playwright + state.json で Cloudflare clearance を維持する。

URL pattern:
    https://www.38north.org/YYYY/MM/<slug>/
"""

from __future__ import annotations

import re
from pathlib import Path

from src.tools.article_model import Article
from src.watchers.playwright_base import PlaywrightWatcher, WatcherState

WATCHER_NAME = "38north"

_WATCHER = PlaywrightWatcher(
    name=WATCHER_NAME,
    target_url="https://www.38north.org/",
    # 38 North トップは記事カードに <h2><a href> の構造。広めに a[href] を取って
    # url_include_pattern で /YYYY/MM/slug/ だけに絞り込む。
    link_selector="a[href]",
    url_include_pattern=re.compile(
        r"^https://www\.38north\.org/\d{4}/\d{2}/[\w\-]+/?$",
    ),
    # category / tag / author / page を除外
    url_exclude_pattern=re.compile(
        r"/(category|tag|author|page|wp-content|wp-json|feed)/",
    ),
    seen_file=Path("data/38north_seen.json"),
    feed_title="38 North (Stimson Center)",
    max_posts_per_run=8,
)


async def fetch_articles(max_count: int = 8) -> list[Article]:
    return await _WATCHER.fetch_articles(max_count)


async def init_seen() -> int:
    return await _WATCHER.init_seen()


def read_state() -> WatcherState:
    return _WATCHER.read_state()
