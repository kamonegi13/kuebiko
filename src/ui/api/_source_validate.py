"""Phase F: feed / sitemap の validation + garbage filter (前 phase の知見集約)。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import feedparser  # type: ignore[import-untyped]

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
    import xml.etree.ElementTree as ET

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
