"""購読ソース 1 件の編集 (取得設定の変更)。

登録 (wizard) と lifecycle 操作 (enable/disable/delete/folder/rename) の間に空いていた
「後から取得設定を直す」経路。サイト移転で feed URL が変わった / HTML 改修で記事リンク
セレクタが壊れた、といった実際に起きる劣化を **削除して登録し直さずに** 直せるようにする。

編集できるのは UI が意味を説明できるフィールドだけ (URL / フォルダ / 有効 / セレクタ /
URL パターン / 1 回あたり取得件数)。表示名は既存の rename_source が担当する。
``type`` や ``language`` のような登録時に決まる派生値は対象外 (transport の整合が崩れる)。

⚠ **rss は URL が識別子**のため、URL 変更は識別子の変更でもある。過去記事は取得時の
feed_url を保持するので、URL を変えると**その時点で運用統計の連続性が切れる** (呼び出し
側が UI で明示する)。sitemap / html_scraper の識別子は ``name`` なので影響しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.sources import source_store
from src.sources.source_store import TransportT

__all__ = ["EditableSource", "SourcePatch", "get_editable", "update_source"]

# 「取得先 URL」に相当するフィールドは transport で異なる。
_URL_FIELD: dict[TransportT, str] = {
    "rss": "url",
    "sitemap": "sitemap_urls",  # list 形式 (先頭を代表 URL として扱う)
    "html_scraper": "listing_url",
}


@dataclass(frozen=True)
class EditableSource:
    """1 ソースの編集可能フィールド (UI フォームの初期値)。"""

    feed_id: str
    transport: str
    display_name: str
    url: str
    folder: str
    enabled: bool
    # transport 固有 (該当しない transport では None = フォームに出さない)
    article_link_selector: str | None = None
    url_include_pattern: str | None = None
    max_posts_per_run: int | None = None
    # rss のみ True (URL 変更が識別子の変更になる = 統計の連続性が切れる)
    url_is_identity: bool = False


@dataclass(frozen=True)
class SourcePatch:
    """編集フォームの入力 (None = 変更しない)。

    表示名は ``_source_manager.rename_source`` が担当する (同じ操作の入口を分けない)。
    """

    url: str | None = None
    folder: str | None = None
    enabled: bool | None = None
    article_link_selector: str | None = None
    url_include_pattern: str | None = None
    max_posts_per_run: int | None = None


def _display_field(transport: TransportT) -> str:
    return "name" if transport == "rss" else "feed_title"


def _url_of(entry: dict[str, Any], transport: TransportT) -> str:
    if transport == "sitemap":
        urls = entry.get("sitemap_urls") or []
        return str(urls[0]) if urls else ""
    return str(entry.get(_URL_FIELD[transport], "") or "")


def _find(feed_id: str) -> tuple[TransportT, str, list[dict[str, Any]], dict[str, Any]] | None:
    """feed_id から (transport, ident, 全 entries, 対象 entry) を引く。"""
    from src.ui.api._source_manager import _key_of, _resolve

    transport, ident = _resolve(feed_id)
    entries = source_store.load_entries(transport)
    for e in entries:
        if _key_of(e, transport) == ident:
            return transport, ident, entries, e
    return None


def get_editable(feed_id: str) -> EditableSource | None:
    """編集フォームの初期値を返す。見つからなければ None。"""
    found = _find(feed_id)
    if found is None:
        return None
    transport, _ident, _entries, entry = found
    name = str(entry.get(_display_field(transport)) or entry.get("name") or "")
    max_posts = entry.get("max_posts_per_run")
    return EditableSource(
        feed_id=feed_id,
        transport=transport,
        display_name=name,
        url=_url_of(entry, transport),
        folder=str(entry.get("folder", "") or ""),
        enabled=bool(entry.get("enabled", True)),
        article_link_selector=(
            str(entry.get("article_link_selector", "") or "")
            if transport == "html_scraper"
            else None
        ),
        # 範囲指定は sitemap / html_scraper の共通概念 (どちらも「サイトの URL 群から
        # 記事だけを選ぶ」問題を持つ)。rss は feed が既に記事の列なので対象外。
        url_include_pattern=(
            str(entry.get("url_include_pattern", "") or "") if transport != "rss" else None
        ),
        max_posts_per_run=(int(max_posts) if isinstance(max_posts, int) else None),
        url_is_identity=transport == "rss",
    )


def _patched(entry: dict[str, Any], transport: TransportT, patch: SourcePatch) -> dict[str, Any]:
    """patch を当てた **新しい** entry を返す (元 entry は変更しない)。"""
    out = dict(entry)
    if patch.url:
        if transport == "sitemap":
            rest = [str(u) for u in (entry.get("sitemap_urls") or [])][1:]
            out["sitemap_urls"] = [patch.url, *rest]
        else:
            out[_URL_FIELD[transport]] = patch.url
    if patch.folder is not None:
        if patch.folder:
            out["folder"] = patch.folder
        else:
            out.pop("folder", None)
    if patch.enabled is not None:
        out["enabled"] = patch.enabled
    if patch.article_link_selector and transport == "html_scraper":
        out["article_link_selector"] = patch.article_link_selector
    if patch.url_include_pattern is not None and transport != "rss":
        if patch.url_include_pattern:
            out["url_include_pattern"] = patch.url_include_pattern
        else:
            out.pop("url_include_pattern", None)
    if patch.max_posts_per_run is not None and transport in ("sitemap", "html_scraper"):
        out["max_posts_per_run"] = patch.max_posts_per_run
    return out


def update_source(feed_id: str, patch: SourcePatch) -> tuple[str, bool]:
    """1 ソースを更新し (更新後の feed_id, 変更があったか) を返す。

    Raises:
        ValueError: 対象が見つからない / 変更後の識別子が他ソースと衝突する
    """
    from src.ui.api._source_manager import _key_of, _to_feed_id

    found = _find(feed_id)
    if found is None:
        raise ValueError("対象のソースが見つかりません")
    transport, ident, entries, entry = found
    updated = _patched(entry, transport, patch)
    if updated == entry:
        return feed_id, False
    new_key = _key_of(updated, transport)
    if not new_key:
        raise ValueError("URL / 名前が空になる変更はできません")
    if new_key != ident and any(_key_of(e, transport) == new_key for e in entries):
        raise ValueError("同じ URL のソースが既に登録されています")
    source_store.save_entries(
        transport,
        [updated if _key_of(e, transport) == ident else e for e in entries],
        note=f"ソース編集: {new_key} (UI)",
    )
    return _to_feed_id(updated, transport), True
