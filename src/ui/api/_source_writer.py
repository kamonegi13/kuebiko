"""Phase F: 統合ソース writer (DB SSoT 化後)。

SourceCandidate.transport に応じて適切な transport の entry list に追加 / 削除する。
保存先は ``source_store`` 経由 (runtime SSoT は config_store/DB、yaml は seed 専用)。
``SOURCES_CONFIG_DB=0`` のときだけ source_store が yaml に直書きする (rollback)。

例外: ``web-scraper-watchers`` cluster の宣言 (pipelines.yaml) は bespoke .py scraper の
持ち場であり本移行の対象外 — ここだけは従来通り yaml を atomic write する。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from src.logging_config import get_logger
from src.sources import source_store
from src.ui.api._source_models import SourceCandidate

_log = get_logger(__name__)

FEEDS_YAML = Path("config/feeds.yaml")
WATCHERS_YAML = Path("config/watchers.yaml")
SCRAPERS_YAML = Path("config/scrapers.yaml")
PIPELINES_YAML = Path("config/pipelines.yaml")
SCRAPER_CLUSTER_NAME = "web-scraper-watchers"


def _atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    """pipelines.yaml (cluster 宣言) 用の atomic write。source entry は source_store 経由。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def _validate_kebab(name: str) -> None:
    if not re.match(r"^[a-z0-9][a-z0-9\-]*$", name):
        raise ValueError(f"name は kebab-case (英小+数+-) のみ: {name!r}")


def _add_to_feeds_yaml(
    name: str,
    feed_url: str,
    folder: str,
    enabled: bool,
    source_name: str,
) -> tuple[int, list[Path]]:
    entries = source_store.load_entries("rss")
    if any(e.get("url") == feed_url for e in entries):
        raise ValueError(f"feed URL は既登録: {feed_url}")
    entry: dict[str, Any] = {
        "name": source_name or name,
        "url": feed_url,
        "enabled": enabled,
    }
    if folder:
        entry["folder"] = folder
    new_entries = entries + [entry]
    source_store.save_entries("rss", new_entries, note=f"RSS 購読を追加: {entry['name']}")
    return len(new_entries), [FEEDS_YAML]


def _add_to_watchers_yaml(
    name: str,
    sitemap_url: str,
    feed_title: str,
    enabled: bool,
    folder: str = "",
) -> tuple[int, list[Path]]:
    """sitemap candidate を watcher entry list に登録。

    include_pattern は user URL の host 前置を default に (subcategory 限定的)。
    """
    _validate_kebab(name)
    entries = source_store.load_entries("sitemap")
    if any(e.get("name") == name for e in entries):
        raise ValueError(f"watcher name は既登録: {name}")

    # default include pattern: sitemap host の article-like URL を緩く allow
    host = urlparse(sitemap_url).netloc
    safe_host = re.escape(host)
    default_include = rf"^https?://{safe_host}/.+"

    entry: dict[str, Any] = {
        "name": name,
        "type": "sitemap",
        "enabled": enabled,
        "sitemap_urls": [sitemap_url],
        "url_include_pattern": default_include,
        "feed_title": feed_title or host,
        "max_posts_per_run": 8,
    }
    if folder:
        entry["folder"] = folder
    new_entries = entries + [entry]
    source_store.save_entries("sitemap", new_entries, note=f"サイトマップ監視を追加: {name}")
    return len(new_entries), [WATCHERS_YAML]


def _add_to_scrapers_yaml(
    name: str,
    candidate: SourceCandidate,
    feed_title: str,
    enabled: bool,
    folder: str = "",
) -> tuple[int, list[Path], bool]:
    """scraper candidate を scraper entry list に追加。

    web_scraper_cluster は enabled 全件を registry から auto-collect するため
    pipelines.yaml への明示 append は不要 (enabled = entry が SSoT)。
    """
    _validate_kebab(name)
    if not candidate.article_link_selector:
        raise ValueError("html_scraper candidate に article_link_selector がありません")

    entries = source_store.load_entries("html_scraper")
    if any(e.get("name") == name for e in entries):
        raise ValueError(f"scraper name は既登録: {name}")
    entry: dict[str, Any] = {
        "name": name,
        "enabled": enabled,
        "listing_url": candidate.fetch_url,
        "article_link_selector": candidate.article_link_selector,
        "feed_title": feed_title or candidate.source_name,
        "max_posts_per_run": 8,
    }
    if candidate.title_selector:
        entry["title_selector"] = candidate.title_selector
    if folder:
        entry["folder"] = folder
    new_entries = entries + [entry]
    # save_entries が registry cache を invalidate する (次 run で反映)。
    source_store.save_entries("html_scraper", new_entries, note=f"HTML 解析を追加: {name}")
    return len(new_entries), [SCRAPERS_YAML], False


def _remove_from_cluster(name: str) -> bool:
    """web-scraper-watchers cluster の source.scrapers から name を除去 (pipelines.yaml)。"""
    if not PIPELINES_YAML.exists():
        return False
    with PIPELINES_YAML.open(encoding="utf-8") as f:
        praw = yaml.safe_load(f) or {}
    target = next(
        (p for p in praw.get("pipelines", []) if p.get("name") == SCRAPER_CLUSTER_NAME),
        None,
    )
    if target is None:
        return False
    source = target.get("source") or {}
    scrapers = source.get("scrapers") or []
    new_list = [s for s in scrapers if s.get("name") != name]
    if len(new_list) == len(scrapers):
        return False
    source["scrapers"] = new_list
    _atomic_write_yaml(PIPELINES_YAML, praw)
    return True


def _remove_named_entry(transport: source_store.TransportT, name: str) -> bool:
    """sitemap / html_scraper の entry list から name のエントリを除去。"""
    entries = source_store.load_entries(transport)
    new_entries = [e for e in entries if e.get("name") != name]
    if len(new_entries) == len(entries):
        return False
    source_store.save_entries(transport, new_entries, note=f"削除: {name}")
    return True


def _remove_from_feeds_yaml(url: str) -> bool:
    """feed entry list から url 一致の feed を除去。"""
    entries = source_store.load_entries("rss")
    new_entries = [e for e in entries if e.get("url") != url]
    if len(new_entries) == len(entries):
        return False
    source_store.save_entries("rss", new_entries, note=f"RSS 購読を削除: {url}")
    return True


def unregister_source(feed_id: str) -> tuple[bool, str | None]:
    """feed_id からソースを特定し、登録の逆操作で削除する。

    feed_id 形式: "scraper:<name>" / "watcher:<name>" / それ以外は rss feed URL。
    Returns: (removed, commit_sha=None)。git は触らない (DB SSoT)。
    """
    if feed_id.startswith("scraper:"):
        name = feed_id[len("scraper:") :]
        removed = _remove_named_entry("html_scraper", name)
        cluster_removed = _remove_from_cluster(name)
        return (removed or cluster_removed), None

    if feed_id.startswith("watcher:"):
        name = feed_id[len("watcher:") :]
        removed = _remove_named_entry("sitemap", name)
        cluster_removed = _remove_from_cluster(name)
        return (removed or cluster_removed), None

    # rss: feed_id == url
    return _remove_from_feeds_yaml(feed_id), None


def register_source(
    candidate: SourceCandidate,
    name: str,
    folder: str,
    enabled: bool,
) -> tuple[str, int, bool, str | None]:
    """transport に応じて適切な entry list に登録する。

    Returns:
        (target_label, total_in_transport, appended_to_cluster, commit_sha=None)
    """
    if candidate.transport in ("rss", "atom"):
        total, _paths = _add_to_feeds_yaml(
            name,
            candidate.fetch_url,
            folder,
            enabled,
            candidate.source_name,
        )
        return str(FEEDS_YAML), total, False, None

    if candidate.transport == "sitemap":
        total, _paths = _add_to_watchers_yaml(
            name,
            candidate.fetch_url,
            candidate.source_name,
            enabled,
            folder,
        )
        return str(WATCHERS_YAML), total, False, None

    if candidate.transport == "html_scraper":
        total, _paths, appended = _add_to_scrapers_yaml(
            name,
            candidate,
            candidate.source_name,
            enabled,
            folder,
        )
        return str(SCRAPERS_YAML), total, appended, None

    raise ValueError(f"対応していないソース種別です: {candidate.transport}")
