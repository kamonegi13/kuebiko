"""config/sources/scrapers.yaml の declarative loader (Phase E-3)。

HTML scraper を yaml_registry と並列で管理。 setup は SitemapWatcher の
yaml_registry を踏襲し、 source_router._resolve_scraper から透過利用される。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger
from src.sources import source_store
from src.tools.article_model import Article
from src.watchers.html_listing import HtmlListingWatcher
from src.watchers.sitemap_base import WatcherState

_log = get_logger(__name__)

DEFAULT_SCRAPERS_YAML = Path("config/sources/scrapers.yaml")
DEFAULT_STATE_DIR = Path("data")


class ScraperDef(BaseModel):
    """yaml の 1 scraper 宣言。"""

    # FeedDef と同様 extra="ignore": 旧 entry の vestigial key (language 等) を黙って捨てる
    model_config = ConfigDict(extra="ignore")

    name: str
    enabled: bool = True
    listing_url: str
    article_link_selector: str
    title_selector: str = ""
    feed_title: str
    max_posts_per_run: int = 8
    url_include_pattern: str = ""
    url_exclude_pattern: str = ""
    # 収集 href の決定論的書換 (regex → replacement、先頭 1 回)。listing が検索フォームの
    # deeplink を返すサイト (BSI の /SiteGlobals/Forms/Suche/… → 404、canonical は
    # prefix 除去で 200) への宣言的対処 (監査 2026-08-01 ①)。
    url_rewrite_pattern: str = ""
    url_rewrite_replacement: str = ""
    folder: str = ""  # organizational 分類軸 (全 transport 共通、非 behavioral)


class ScrapersConfig(BaseModel):
    """config/sources/scrapers.yaml の root schema。"""

    model_config = ConfigDict(extra="forbid")

    scrapers: list[ScraperDef] = Field(default_factory=list)


def load_scrapers_config(path: Path | None = None) -> ScrapersConfig:
    """scraper list を load (runtime SSoT は config_store/DB、yaml は seed 専用)。

    ``path`` 明示時のみ yaml を直読み (テスト / seed)。詳細は src/sources/source_store.py。
    """
    entries = source_store.load_entries("html_scraper", path=path)
    return ScrapersConfig.model_validate({"scrapers": entries})


def _state_file_for(name: str, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    safe = name.replace("-", "_")
    return state_dir / f"{safe}_seen.json"


def build_html_listing_watcher(
    d: ScraperDef,
    state_dir: Path = DEFAULT_STATE_DIR,
) -> HtmlListingWatcher:
    return HtmlListingWatcher(
        name=d.name,
        listing_url=d.listing_url,
        article_link_selector=d.article_link_selector,
        title_selector=d.title_selector,
        state_file=_state_file_for(d.name, state_dir),
        feed_title=d.feed_title,
        max_posts_per_run=d.max_posts_per_run,
        url_include_pattern=d.url_include_pattern,
        url_exclude_pattern=d.url_exclude_pattern,
        url_rewrite_pattern=d.url_rewrite_pattern,
        url_rewrite_replacement=d.url_rewrite_replacement,
    )


_registry_cache: dict[str, HtmlListingWatcher] | None = None


def get_registry() -> dict[str, HtmlListingWatcher]:
    global _registry_cache  # noqa: PLW0603
    if _registry_cache is None:
        cfg = load_scrapers_config()
        _registry_cache = {d.name: build_html_listing_watcher(d) for d in cfg.scrapers if d.enabled}
        _log.info(
            "scrapers_registry_loaded",
            count=len(_registry_cache),
            names=sorted(_registry_cache.keys()),
        )
    return _registry_cache


def reset_cache() -> None:
    global _registry_cache  # noqa: PLW0603
    _registry_cache = None


async def fetch_articles_by_name(name: str, max_count: int = 8) -> list[Article]:
    registry = get_registry()
    watcher = registry.get(name)
    if watcher is None:
        raise ValueError(f"scrapers registry に未登録: {name}")
    return await watcher.fetch_articles(max_count=max_count)


def read_state_by_name(name: str) -> WatcherState | None:
    registry = get_registry()
    watcher = registry.get(name)
    return watcher.read_state() if watcher else None


__all__ = [
    "ScraperDef",
    "ScrapersConfig",
    "build_html_listing_watcher",
    "fetch_articles_by_name",
    "get_registry",
    "load_scrapers_config",
    "read_state_by_name",
    "reset_cache",
]
