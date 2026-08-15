"""yaml 駆動の watcher registry (Phase X-2: site .py の廃止)。

旧 src/watchers/{enisa,ipa,ccdcoe,...}.py の per-site インスタンス化を
config/sources/watchers.yaml の declarative 宣言に集約する。framework
(SitemapWatcher in sitemap_base.py) は core に残す。

設計方針:
- yaml load は eager (module 起動時)、cache する
- 旧 site .py との後方互換: name で lookup できれば yaml registry を優先
- 公開 repo 公開時は config/sources/watchers.yaml を sample (空 or 代表 3-5 件) に差替
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger
from src.sources import source_store
from src.tools.article_model import Article
from src.watchers.sitemap_base import SitemapWatcher, WatcherState

_log = get_logger(__name__)

DEFAULT_WATCHERS_YAML = Path("config/sources/watchers.yaml")
DEFAULT_STATE_DIR = Path("data")


class WatcherDef(BaseModel):
    """yaml の 1 watcher 宣言。"""

    # FeedDef と同様 extra="ignore": 旧 entry の vestigial key (language 等) を黙って捨てる
    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["sitemap"] = "sitemap"
    enabled: bool = True
    sitemap_urls: list[str] = Field(default_factory=list)
    url_include_pattern: str
    url_exclude_pattern: str | None = None
    feed_title: str
    max_posts_per_run: int = 10
    folder: str = ""  # organizational 分類軸 (全 transport 共通、非 behavioral)


class WatchersConfig(BaseModel):
    """config/sources/watchers.yaml の root schema。"""

    model_config = ConfigDict(extra="forbid")

    watchers: list[WatcherDef] = Field(default_factory=list)


def load_watchers_config(path: Path | None = None) -> WatchersConfig:
    """watcher list を load (runtime SSoT は config_store/DB、yaml は seed 専用)。

    ``path`` 明示時のみ yaml を直読み (テスト / seed)。詳細は src/sources/source_store.py。
    """
    entries = source_store.load_entries("sitemap", path=path)
    return WatchersConfig.model_validate({"watchers": entries})


def _state_file_for(name: str, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    """name → state.json path (snake_case で統一)。"""
    safe = name.replace("-", "_")
    return state_dir / f"{safe}_seen.json"


def build_sitemap_watcher(d: WatcherDef, state_dir: Path = DEFAULT_STATE_DIR) -> SitemapWatcher:
    """yaml entry → SitemapWatcher instance を生成。"""
    if d.type != "sitemap":
        raise ValueError(f"unsupported watcher type for now: {d.type}")
    include_re = re.compile(d.url_include_pattern)
    exclude_re = re.compile(d.url_exclude_pattern) if d.url_exclude_pattern else None
    return SitemapWatcher(
        name=d.name,
        sitemap_urls=tuple(d.sitemap_urls),
        url_include_pattern=include_re,
        url_exclude_pattern=exclude_re,
        state_file=_state_file_for(d.name, state_dir),
        feed_title=d.feed_title,
        max_posts_per_run=d.max_posts_per_run,
    )


# モジュールレベル cache (yaml 編集後は再起動で反映)
_registry_cache: dict[str, SitemapWatcher] | None = None


def get_registry() -> dict[str, SitemapWatcher]:
    """name → SitemapWatcher mapping。enabled な watcher のみ含む。"""
    global _registry_cache  # noqa: PLW0603
    if _registry_cache is None:
        cfg = load_watchers_config()
        _registry_cache = {d.name: build_sitemap_watcher(d) for d in cfg.watchers if d.enabled}
        _log.info(
            "yaml_watcher_registry_loaded",
            count=len(_registry_cache),
            names=sorted(_registry_cache.keys()),
        )
    return _registry_cache


def reset_cache() -> None:
    """テスト用に cache をリセット。"""
    global _registry_cache  # noqa: PLW0603
    _registry_cache = None


async def fetch_articles_by_name(name: str, max_count: int = 10) -> list[Article]:
    """yaml registry から watcher を引いて fetch_articles を実行。

    name が yaml に存在しない場合は ValueError。
    """
    registry = get_registry()
    watcher = registry.get(name)
    if watcher is None:
        raise ValueError(f"yaml watcher registry に未登録: {name}")
    return await watcher.fetch_articles(max_count=max_count)


def read_state_by_name(name: str) -> WatcherState | None:
    """yaml registry の watcher の state snapshot を返す。"""
    registry = get_registry()
    watcher = registry.get(name)
    return watcher.read_state() if watcher else None


__all__ = [
    "WatcherDef",
    "WatchersConfig",
    "build_sitemap_watcher",
    "fetch_articles_by_name",
    "get_registry",
    "load_watchers_config",
    "read_state_by_name",
    "reset_cache",
]
