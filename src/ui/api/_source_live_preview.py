"""登録済みソースのライブプレビュー。

一覧でソースをクリックした際、今もそのソースが機能しているか (記事が取れるか)
を確認するため最新情報をライブ取得する。

保存前の取得テスト (``preview_url``) も同じ描画を使う。編集途中の URL は config に
まだ無いため、こちらだけは request 由来の URL を fetch する (登録ウィザードの
discover / preview_html_listing と同じ扱い)。

セキュリティ: 登録済みソースの確認 (``preview_subscription``) は fetch 先を config
(feeds/scrapers/watchers.yaml) から解決する。保存前テストを含め **全経路で
url_guard の public 検証を通す** (private / loopback / metadata は fetch しない)。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from src.sources import source_store
from src.tools.direct_rss_source import load_feeds_config
from src.tools.url_guard import assert_safe_public_url
from src.ui.api._source_html_preview import preview_html_listing_explicit
from src.ui.api._source_http import detect_kind, fetch_bytes, fetch_text
from src.ui.api._source_models import LivePreviewResponse, PreviewArticle
from src.ui.api._source_validate import (
    fetch_page_title,
    parse_sitemap_locs,
    validate_feed_body,
)

_MAX_ITEMS = 5


def _find_entry(transport: source_store.TransportT, name: str) -> dict[str, Any] | None:
    """登録済み entry を SSoT (config_store/DB or seed yaml) から name で引く。"""
    for e in source_store.load_entries(transport):
        if e.get("name") == name:
            return e
    return None


def _preview_rss(feed_url: str) -> LivePreviewResponse:
    # 登録済み feed のみ許可 (任意 URL fetch を防ぐ)
    cfg = load_feeds_config()
    if not any(f.url == feed_url for f in cfg.feeds):
        return LivePreviewResponse(ok=False, error="未登録の feed です")
    return _preview_rss_url(feed_url)


def _preview_rss_url(feed_url: str) -> LivePreviewResponse:
    """feed URL を直接取得して中身を返す (登録の有無を問わない)。"""
    assert_safe_public_url(feed_url)
    status, body, headers, stage = fetch_text(feed_url)
    if status >= 400:
        return LivePreviewResponse(
            ok=False, kind="rss", checked_url=feed_url, error=f"HTTP {status}", fetch_stage=stage
        )
    _valid, _title, _n, _last, preview = validate_feed_body(body, _MAX_ITEMS)
    kind = detect_kind(body, headers)
    return LivePreviewResponse(
        ok=len(preview) > 0,
        kind=kind or "rss",
        checked_url=feed_url,
        items=list(preview),
        error=None if preview else "記事が取得できませんでした (feed が空 / 形式変化の可能性)",
        fetch_stage=stage,
    )


async def _preview_scraper(name: str) -> LivePreviewResponse:
    e = _find_entry("html_scraper", name)
    if e is None:
        return LivePreviewResponse(ok=False, error=f"HTML 解析の設定『{name}』が見つかりません")
    listing_url = str(e.get("listing_url", ""))
    selector = str(e.get("article_link_selector", ""))
    assert_safe_public_url(listing_url)
    candidate = await preview_html_listing_explicit(
        listing_url,
        article_link_selector=selector,
        title_selector=str(e.get("title_selector", "")),
        max_preview=_MAX_ITEMS,
    )
    items = list(candidate.preview_articles)
    return LivePreviewResponse(
        ok=len(items) > 0,
        kind="html_scraper",
        checked_url=listing_url,
        items=items,
        error=(
            None
            if items
            else "抽出ルールで記事を取得できません (サイト構造が変わった可能性があります)"
        ),
    )


def _preview_sitemap(name: str) -> LivePreviewResponse:
    e = _find_entry("sitemap", name)
    if e is None:
        return LivePreviewResponse(
            ok=False, error=f"サイトマップ監視の設定『{name}』が見つかりません"
        )
    urls = e.get("sitemap_urls") or []
    if not urls:
        return LivePreviewResponse(ok=False, error="sitemap_urls が空です")
    return _preview_sitemap_url(str(urls[0]), str(e.get("url_include_pattern", "")))


def _lastmod_sort_key(lastmod: datetime | None) -> tuple[bool, datetime]:
    """lastmod を比較可能な key に正規化する (None は最後)。

    ⚠ **同一 sitemap 内に naive と aware が混在しうる** — ``<lastmod>2026-07-30</lastmod>``
    (日付のみ = naive) と ``2026-07-30T12:00:00+00:00`` (TZ 付き = aware) が同じファイルに
    並ぶサイトが実在する (実測: 261 件中 naive 134 / aware 127)。素の datetime 比較は
    ``TypeError: can't compare offset-naive and offset-aware datetimes`` になり、
    **取得テストが必須ゲートなのでそのソースを登録できなくなる**。naive は UTC とみなす。
    """
    if lastmod is None:
        return (False, datetime.min.replace(tzinfo=UTC))
    return (True, lastmod if lastmod.tzinfo else lastmod.replace(tzinfo=UTC))


def _preview_sitemap_url(sitemap_url: str, pattern: str) -> LivePreviewResponse:
    """サイトマップ URL を直接取得し、pattern に一致する URL を記事として返す。"""
    assert_safe_public_url(sitemap_url)
    status, body, _, stage = fetch_bytes(sitemap_url)
    if status >= 400:
        return LivePreviewResponse(
            ok=False,
            kind="sitemap",
            checked_url=sitemap_url,
            error=f"HTTP {status}",
            fetch_stage=stage,
        )
    locs = parse_sitemap_locs(body)
    matched = [(u, lm) for (u, lm) in locs if not pattern or re.search(pattern, u)]
    # lastmod 降順 (None は最後) で「最新」を優先。document 順だと evergreen
    # ページ (websecurity 解説等) が並び、最新の注意喚起が出ない。
    matched.sort(key=lambda x: _lastmod_sort_key(x[1]), reverse=True)
    items = [
        PreviewArticle(
            title=fetch_page_title(u) or (u.rstrip("/").split("/")[-1] or u),
            url=u,
            published=lm,
        )
        for (u, lm) in matched[:_MAX_ITEMS]
    ]
    return LivePreviewResponse(
        ok=len(items) > 0,
        kind="sitemap",
        checked_url=sitemap_url,
        items=items,
        error=None if items else "include pattern に一致する URL がありません",
        fetch_stage=stage,
    )


async def preview_subscription(feed_id: str) -> LivePreviewResponse:
    """feed_id からソースを特定し最新情報をライブ取得する。

    feed_id 形式: "scraper:<name>" / "watcher:<name>" / それ以外は rss feed URL。
    """
    if feed_id.startswith("scraper:"):
        return await _preview_scraper(feed_id[len("scraper:") :])
    if feed_id.startswith("watcher:"):
        return _preview_sitemap(feed_id[len("watcher:") :])
    return _preview_rss(feed_id)


def preview_url(kind: str, url: str, *, url_include_pattern: str = "") -> LivePreviewResponse:
    """**保存前**の取得テスト: 編集中の URL をその場で取得して中身を返す。

    編集途中の URL は config にまだ無いため、``preview_subscription`` と違い
    request 由来の URL を fetch する (登録ウィザードと同じ扱い)。SSRF は
    ``assert_safe_public_url`` + fetch 層の guard で塞ぐ。

    html_scraper はセレクタと一体で確認する必要があるため対象外
    (UI は ``preview_html_listing_explicit`` を使う)。
    """
    if kind == "sitemap":
        return _preview_sitemap_url(url, url_include_pattern)
    return _preview_rss_url(url)
