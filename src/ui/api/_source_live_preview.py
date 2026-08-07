"""登録済みソースのライブプレビュー。

一覧でソースをクリックした際、今もそのソースが機能しているか (記事が取れるか)
を確認するため最新情報をライブ取得する。

セキュリティ: fetch 先 URL は request ではなく config (feeds/scrapers/watchers.yaml)
から解決する。任意 URL を fetch させないことで SSRF を防ぐ。さらに解決した URL も
url_guard で public 検証する。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

from src.sources import source_store
from src.tools.direct_rss_source import load_feeds_config
from src.tools.url_guard import assert_safe_public_url
from src.ui.api._source_html_preview import preview_html_listing_explicit
from src.ui.api._source_http import detect_kind, fetch_bytes, fetch_text
from src.ui.api._source_models import LivePreviewResponse, PreviewArticle
from src.ui.api._source_validate import validate_feed_body

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


def _parse_sitemap_locs_with_lastmod(body: bytes) -> list[tuple[str, datetime | None]]:
    """sitemap urlset から (loc, lastmod) を抽出。lastmod は ISO8601 を parse。"""
    out: list[tuple[str, datetime | None]] = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return out
    for url_el in root.findall(".//{*}url"):
        loc_el = url_el.find("{*}loc")
        if loc_el is None or not loc_el.text:
            continue
        loc = loc_el.text.strip()
        lastmod: datetime | None = None
        lm_el = url_el.find("{*}lastmod")
        if lm_el is not None and lm_el.text:
            try:
                lastmod = datetime.fromisoformat(lm_el.text.strip())
            except ValueError:
                lastmod = None
        out.append((loc, lastmod))
    return out


def _fetch_page_title(url: str) -> str | None:
    """ページの <title> を best-effort で取得 (sitemap は title を持たないため)。"""
    try:
        status, html, _, _ = fetch_text(url, timeout=8.0)
    except Exception:  # noqa: BLE001
        return None
    if status >= 400 or not html:
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    # 「ページ名 | IPA ...」のサイト名 suffix を落とす
    title = re.split(r"\s*[|｜]\s*", title)[0].strip()
    return title[:120] or None


def _preview_sitemap(name: str) -> LivePreviewResponse:
    e = _find_entry("sitemap", name)
    if e is None:
        return LivePreviewResponse(
            ok=False, error=f"サイトマップ監視の設定『{name}』が見つかりません"
        )
    urls = e.get("sitemap_urls") or []
    if not urls:
        return LivePreviewResponse(ok=False, error="sitemap_urls が空です")
    sitemap_url = str(urls[0])
    pattern = str(e.get("url_include_pattern", ""))
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
    locs = _parse_sitemap_locs_with_lastmod(body)
    matched = [(u, lm) for (u, lm) in locs if not pattern or re.search(pattern, u)]
    # lastmod 降順 (None は最後) で「最新」を優先。document 順だと evergreen
    # ページ (websecurity 解説等) が並び、最新の注意喚起が出ない。
    matched.sort(
        key=lambda x: (x[1] is not None, x[1] or datetime.min.replace(tzinfo=None)),
        reverse=True,
    )
    items = [
        PreviewArticle(
            title=_fetch_page_title(u) or (u.rstrip("/").split("/")[-1] or u),
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
