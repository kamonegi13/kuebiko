"""統合ソース管理層 (Phase F+、DB SSoT 化後)。

RSS / sitemap / html_scraper を transport 透過に list / enable / disable / delete /
folder / rename する。lifecycle 管理では 3 transport は「名前 + enabled を持つソース」
という同一抽象であり、保存先 (config_store の key) や fetch 機構の違いは ``source_store``
が吸収する。

feed_id 規約: "scraper:<name>" / "watcher:<name>" / それ以外は rss feed URL。
runtime SSoT は config_store (DB)。enabled は entry が単一 SSoT (web_scraper_cluster が
registry から auto-collect)。複数ソースへの一括操作は **transport 単位で 1 save に集約**
する (履歴を綺麗に保つ)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.sources import source_store
from src.sources.source_store import TransportT
from src.tools.direct_rss_source import load_feeds_config

__all__ = [
    "BulkActionT",
    "ManagedSource",
    "bulk",
    "list_sources",
    "rename_source",
    "set_folder",
]

BulkActionT = Literal["enable", "disable", "delete"]

_PREFIX: dict[str, TransportT] = {"scraper:": "html_scraper", "watcher:": "sitemap"}


@dataclass(frozen=True)
class ManagedSource:
    """transport 透過なソース 1 件 (UI 一覧用)。"""

    feed_id: str
    title: str  # = Article.feed_title (stats join key)
    url: str
    transport: str  # rss / sitemap / html_scraper
    enabled: bool
    folder: str = ""


def _resolve(feed_id: str) -> tuple[TransportT, str]:
    """feed_id → (transport, ident)。ident は rss=url、 sitemap/html_scraper=name。"""
    for pfx, transport in _PREFIX.items():
        if feed_id.startswith(pfx):
            return transport, feed_id[len(pfx) :]
    return "rss", feed_id


def _key_of(entry: dict[str, Any], transport: TransportT) -> str:
    """transport の一致キー (rss=url / 他=name) を返す。"""
    return str(entry.get("url" if transport == "rss" else "name", ""))


def _group(feed_ids: list[str]) -> dict[TransportT, list[str]]:
    """feed_id 群を transport 別の ident list に振り分ける。"""
    out: dict[TransportT, list[str]] = {}
    for fid in feed_ids:
        transport, ident = _resolve(fid)
        out.setdefault(transport, []).append(ident)
    return out


def list_sources() -> list[ManagedSource]:
    """全 transport の全ソース (無効含む) を返す。"""
    out: list[ManagedSource] = []
    try:
        for f in load_feeds_config().feeds:
            out.append(
                ManagedSource(
                    feed_id=f.url,
                    title=f.name,
                    url=f.url,
                    transport="rss",
                    enabled=f.enabled,
                    folder=f.folder or "",
                )
            )
    except Exception:  # noqa: BLE001
        pass
    for e in source_store.load_entries("html_scraper"):
        name = str(e.get("name", ""))
        out.append(
            ManagedSource(
                feed_id=f"scraper:{name}",
                title=str(e.get("feed_title") or name),
                url=str(e.get("listing_url", "")),
                transport="html_scraper",
                enabled=bool(e.get("enabled", True)),
                folder=str(e.get("folder", "") or ""),
            )
        )
    for e in source_store.load_entries("sitemap"):
        name = str(e.get("name", ""))
        urls = e.get("sitemap_urls") or []
        out.append(
            ManagedSource(
                feed_id=f"watcher:{name}",
                title=str(e.get("feed_title") or name),
                url=str(urls[0]) if urls else "",
                transport="sitemap",
                enabled=bool(e.get("enabled", True)),
                folder=str(e.get("folder", "") or ""),
            )
        )
    return out


# --- in-memory mutators (単一 entry を変異、 changed を返す) ---------------------


def _apply_enabled(entry: dict[str, Any], enabled: bool) -> bool:
    if entry.get("enabled") != enabled:
        entry["enabled"] = enabled
        return True
    return False


def _apply_folder(entry: dict[str, Any], folder: str) -> bool:
    if folder:
        if entry.get("folder") != folder:
            entry["folder"] = folder
            return True
        return False
    if "folder" in entry:
        del entry["folder"]
        return True
    return False


def _apply_display_name(entry: dict[str, Any], transport: TransportT, name: str) -> bool:
    # 表示名フィールドは transport で異なる (rss=name / sitemap・html_scraper=feed_title)。
    field = "name" if transport == "rss" else "feed_title"
    if entry.get(field) != name:
        entry[field] = name
        return True
    return False


# --- public ops (transport 単位で load → 変異 → 1 save) -------------------------


def set_folder(feed_ids: list[str], folder: str) -> tuple[int, str | None]:
    """全 transport の folder を 1 値に統一 (空文字で folder 解除)。"""
    affected = 0
    for transport, idents in _group(feed_ids).items():
        idset = set(idents)
        entries = source_store.load_entries(transport)
        n = sum(1 for e in entries if _key_of(e, transport) in idset and _apply_folder(e, folder))
        if n:
            source_store.save_entries(
                transport, entries, note=f"フォルダを『{folder}』に設定 ({n} 件)"
            )
            affected += n
    return affected, None


def rename_source(feed_id: str, display_name: str) -> tuple[int, str | None]:
    """購読一覧の表示名を変更する (kebab の内部 ID は不変)。"""
    name = display_name.strip()
    if not name:
        raise ValueError("表示名は空にできません")
    transport, ident = _resolve(feed_id)
    entries = source_store.load_entries(transport)
    changed = any(
        _key_of(e, transport) == ident and _apply_display_name(e, transport, name) for e in entries
    )
    if not changed:
        return 0, None
    source_store.save_entries(transport, entries, note=f"表示名変更: {name} (UI)")
    return 1, None


def bulk(feed_ids: list[str], action: BulkActionT) -> tuple[int, str | None]:
    """複数ソースに enable / disable / delete を適用 (transport 単位で 1 save)。

    Returns: (affected_count, commit_sha=None)。git は触らない (DB SSoT)。
    """
    affected = 0
    for transport, idents in _group(feed_ids).items():
        idset = set(idents)
        entries = source_store.load_entries(transport)
        if action == "delete":
            kept = [e for e in entries if _key_of(e, transport) not in idset]
            n = len(entries) - len(kept)
            if n:
                source_store.save_entries(transport, kept, note=f"削除 {n} 件 (bulk UI)")
                affected += n
        else:
            enabled = action == "enable"
            n = sum(
                1 for e in entries if _key_of(e, transport) in idset and _apply_enabled(e, enabled)
            )
            if n:
                action_label = "有効化" if enabled else "無効化"
                source_store.save_entries(
                    transport, entries, note=f"{action_label} {n} 件 (bulk UI)"
                )
                affected += n
    return affected, None
