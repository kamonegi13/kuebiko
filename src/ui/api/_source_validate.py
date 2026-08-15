"""Phase F: feed / sitemap の validation + garbage filter (前 phase の知見集約)。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import feedparser  # type: ignore[import-untyped]

from src.ui.api._source_http import fetch_text
from src.ui.api._source_models import PreviewArticle

# Phase E-4: 404 placeholder pattern
_GARBAGE_URL_PATTERNS = (
    "/not-found",
    "/404",
    "/error",
    "/no-content",
    "/page-not-found",
)
_GARBAGE_TITLE_PATTERNS = (
    "content not found",
    "page not found",
    "404",
    "not found",
    "no content",
    "error",
    "ページが見つかりません",
)

# Phase E-4: sitemap nav-only page pattern
_NAV_PATH_RE = re.compile(
    r"^/?(authors?|categories?|tags?|archive|search|page|not[\-_]?found|"
    r"404|robots|sitemap|index|home|contact|about|feed|rss)/?$",
    re.IGNORECASE,
)


def is_garbage_entry(entry: Any) -> bool:
    link = (entry.get("link") or "").lower()
    title = (entry.get("title") or "").lower().strip()
    if any(p in link for p in _GARBAGE_URL_PATTERNS):
        return True
    if title:
        for p in _GARBAGE_TITLE_PATTERNS:
            if title == p or title.startswith(p + " ") or title.endswith(" " + p):
                return True
    return False


def is_nav_url(url: str) -> bool:
    try:
        p = urlparse(url).path
    except Exception:  # noqa: BLE001
        return False
    if not p or p == "/":
        return True
    if _NAV_PATH_RE.match(p):
        return True
    return "/not-found" in p or "/page-not-found" in p


def validate_feed_body(
    body: str, max_preview: int = 5
) -> tuple[
    bool,
    str | None,
    int,
    datetime | None,
    list[PreviewArticle],
]:
    """feedparser でパース → (has_real_entries, title, count, last_updated, preview_articles)。

    Phase E-4 + F: garbage entry 除外 + PreviewArticle で uniform output。
    """
    parsed = feedparser.parse(body)
    entries = parsed.entries if hasattr(parsed, "entries") else []
    title = ""
    if hasattr(parsed, "feed"):
        title = parsed.feed.get("title", "") or ""
    real = [e for e in entries if not is_garbage_entry(e)]

    last_updated: datetime | None = None
    preview: list[PreviewArticle] = []
    for e in real:
        published_dt: datetime | None = None
        for key in ("published_parsed", "updated_parsed"):
            st = e.get(key)
            if st:
                try:
                    published_dt = datetime(st[0], st[1], st[2], st[3], st[4], st[5], tzinfo=UTC)
                    break
                except (TypeError, ValueError):
                    continue
        if published_dt and (last_updated is None or published_dt > last_updated):
            last_updated = published_dt
        if len(preview) < max_preview:
            link = (e.get("link") or "").strip()
            t = (e.get("title") or "").strip()
            summary = ""
            for key in ("summary", "description"):
                v = e.get(key)
                if v:
                    summary = re.sub(r"<[^>]+>", " ", str(v))
                    summary = re.sub(r"\s+", " ", summary).strip()
                    break
            if link and t:
                preview.append(
                    PreviewArticle(
                        title=t[:200],
                        url=link[:500],
                        published=published_dt,
                        summary_preview=summary[:200],
                    )
                )
    return bool(real), title or None, len(real), last_updated, preview


def parse_sitemap_xml(body: bytes) -> tuple[str, list[str], list[str]]:
    """sitemap XML を parse。 (kind, urls, sub_sitemaps)。

    kind: "urlset" | "sitemapindex" | "unknown"
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "unknown", [], []
    tag = root.tag.split("}")[-1]
    urls: list[str] = []
    subs: list[str] = []
    if tag == "urlset":
        for loc in root.findall(".//{*}url/{*}loc"):
            if loc.text:
                urls.append(loc.text.strip())
        return "urlset", urls, subs
    if tag == "sitemapindex":
        for loc in root.findall(".//{*}sitemap/{*}loc"):
            if loc.text:
                subs.append(loc.text.strip())
        return "sitemapindex", urls, subs
    return "unknown", [], []


def parse_sitemap_locs(body: bytes) -> list[tuple[str, datetime | None]]:
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


def fetch_page_title(url: str) -> str | None:
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
