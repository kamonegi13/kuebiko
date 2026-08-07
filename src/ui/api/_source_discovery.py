"""Phase F: 統合 source discovery。

入力 URL に対して RSS / Atom / sitemap 全てを並列 probe + preview 抽出。
SourceCandidate の uniform schema で結果を返す。

設計原則:
- 全 candidate に preview_articles が付く (user の意図確認 gate)
- transport を field で区別 (UI 側で見えない)
- quality 指標で並列ランク
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

from src.logging_config import get_logger
from src.ui.api._source_http import (
    INTERACTIVE_HTTP_TIMEOUT,
    SourceFetchError,
    detect_kind,
    fetch_bytes,
    fetch_text,
)
from src.ui.api._source_models import (
    PreviewArticle,
    Quality,
    SourceCandidate,
    TransportLiteral,
)
from src.ui.api._source_validate import (
    is_nav_url,
    parse_sitemap_xml,
    validate_feed_body,
)

_log = get_logger(__name__)

# 拡張 common path (E-1 〜 E-5c 知見)
COMMON_FEED_PATHS = (
    "/feed/",
    "/feed",
    "/rss/",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/feed.xml",
    "/index.xml",
    "/feeds/posts/default",
    "/rss.rdf",
    "/feed.rdf",
    "/index.rdf",
    "/blog/feed/",
    "/blog/feed",
    "/blog/rss/",
    "/blog/rss.xml",
    "/blog/atom.xml",
    "/blog.xml",
    "/news/feed/",
    "/news/rss/",
    "/news/rss.xml",
    "/posts/index.xml",
    "/posts.xml",
)
COMMON_FEED_SUBDOMAINS = ("rss", "feed", "feeds")
COMMON_SITEMAP_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap/",
    "/wp-sitemap.xml",
)
CATEGORY_PATHS = (
    "/feed/",
    "/feed",
    "/rss/",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/feed.xml",
    "/index.xml",
    "/rss.rdf",
)
_FEED_BODY_PREFIXES = ("<?xml", "<rss", "<feed", "<atom", "<rdf:rdf")


def _path_display(url: str) -> str:
    """URL から path 部分を取り出して表示用に整形。"""
    try:
        u = urlparse(url)
        return (u.path or "/") + (("?" + u.query) if u.query else "")
    except Exception:  # noqa: BLE001
        return url


def _compute_quality(entries_count: int, last_updated: datetime | None) -> Quality:
    """entries 数 + 最終更新からヘルススコアを算出 (0-100)。"""
    freshness_days: int | None = None
    if last_updated is not None:
        delta = datetime.now(UTC) - last_updated
        freshness_days = max(0, delta.days)
    # 量 (max 40 点): 20 entries で満点
    volume_score = min(entries_count / 20.0, 1.0) * 40
    # 鮮度 (max 60 点): 7 日内 60、 30 日内 50、 90 日内 30、 1 年内 10、 超過 0
    if freshness_days is None:
        fresh_score = 30  # 不明は中立
    elif freshness_days <= 7:
        fresh_score = 60
    elif freshness_days <= 30:
        fresh_score = 50
    elif freshness_days <= 90:
        fresh_score = 30
    elif freshness_days <= 365:
        fresh_score = 10
    else:
        fresh_score = 0
    score = int(round(volume_score + fresh_score))
    return Quality(
        entries_count=entries_count,
        freshness_days=freshness_days,
        health_score=max(0, min(100, score)),
    )


def _validate_and_build_feed_candidate(
    url: str,
    kind: str,
    body: str,
    detected_via: str,
    max_preview: int,
) -> SourceCandidate | None:
    """feed body を validate して SourceCandidate を組み立てる。 invalid なら None。"""
    if kind not in ("rss", "atom"):
        return None
    valid, title, n, last_updated, preview = validate_feed_body(body, max_preview)
    if not valid:
        return None
    transport: TransportLiteral = kind  # type: ignore[assignment]
    return SourceCandidate(
        transport=transport,
        fetch_url=url,
        source_name=title or _path_display(url),
        path_display=_path_display(url),
        last_updated=last_updated,
        quality=_compute_quality(n, last_updated),
        preview_articles=preview,
        detected_via=detected_via,
    )


def _fetch_and_build_feed(url: str, detected_via: str, max_preview: int) -> SourceCandidate | None:
    """URL を fetch + validate して feed SourceCandidate を返す。"""
    try:
        status, body, headers, _stage = fetch_text(url)
    except Exception:  # noqa: BLE001
        return None
    body_head = body[:100].lstrip().lower() if body else ""
    looks_like_feed = any(body_head.startswith(p) for p in _FEED_BODY_PREFIXES)
    if status >= 400 and not looks_like_feed:
        return None
    kind = detect_kind(body, headers)
    if kind not in ("rss", "atom"):
        return None
    return _validate_and_build_feed_candidate(url, kind, body, detected_via, max_preview)


def _extract_alternate_links(html: str, base_url: str) -> list[tuple[str, str | None, str]]:
    """HTML から alternate feed link を抽出。 (url, title, kind) tuple list。"""
    out: list[tuple[str, str | None, str]] = []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return out
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    for link in soup.find_all("link"):
        href = link.get("href")
        if not href:
            continue
        rel_raw: object = link.get("rel") or []
        rel_list = [rel_raw] if isinstance(rel_raw, str) else list(rel_raw)  # type: ignore[call-overload]
        rels = {str(r).lower() for r in rel_list}
        type_attr = str(link.get("type") or "").lower()
        kind: str | None = None
        if "alternate" in rels:
            if "rss" in type_attr:
                kind = "rss"
            elif "atom" in type_attr:
                kind = "atom"
            elif "rdf" in type_attr:
                kind = "rss"
        elif "feed" in rels:
            kind = "rss"
        if kind is None:
            continue
        full = urljoin(base_url, str(href))
        if full in seen:
            continue
        seen.add(full)
        link_title = str(link.get("title") or "").strip() or None
        out.append((full, link_title, kind))
    # body の <a> も補完 (head に link 宣言ない site)
    a_pattern = re.compile(
        r"(\.xml|\.rss|\.rdf|\.atom)(\?.*)?$|/feed/?$|/rss/?$|/atom/?$",
        re.IGNORECASE,
    )
    a_found = 0
    for a in soup.find_all("a", href=True):
        if a_found >= 8:
            break
        href = str(a["href"])
        if not a_pattern.search(href):
            continue
        full = urljoin(base_url, href)
        try:
            if urlparse(full).netloc != urlparse(base_url).netloc:
                continue
        except Exception:  # noqa: BLE001
            continue
        if full in seen:
            continue
        seen.add(full)
        anchor_text = (a.get_text(" ", strip=True) or "")[:80] or None
        out.append((full, anchor_text, "rss"))  # kind は実 fetch で再判定される
        a_found += 1
    return out


def _build_sitemap_candidate(
    url: str,
    body: bytes,
    detected_via: str,
    max_preview: int,
) -> SourceCandidate | None:
    """sitemap XML を parse → SourceCandidate。 nav-only / 空なら None。"""
    kind, urls, subs = parse_sitemap_xml(body)
    if kind == "unknown":
        return None
    if kind == "urlset":
        real_urls = [u for u in urls if not is_nav_url(u)]
        if not real_urls:
            return None
        # sitemap の preview は先頭 5 URL を「title 不明、 URL のみ」 として展開
        # (trafilatura 抽出は重いので preview には含めない、 register 後の pipeline が処理)
        preview = [
            PreviewArticle(title=_path_display(u).split("/")[-1] or u, url=u)
            for u in real_urls[:max_preview]
        ]
        host = urlparse(url).netloc
        return SourceCandidate(
            transport="sitemap",
            fetch_url=url,
            source_name=f"{host} sitemap ({len(real_urls)} URLs)",
            path_display=_path_display(url),
            last_updated=None,
            quality=_compute_quality(len(real_urls), None),
            preview_articles=preview,
            detected_via=detected_via,
        )
    if kind == "sitemapindex":
        if not subs:
            return None
        # index は sub-sitemap への参照のみ、 preview は sub 名で代替
        preview = [
            PreviewArticle(title=_path_display(s).split("/")[-1] or s, url=s)
            for s in subs[:max_preview]
        ]
        host = urlparse(url).netloc
        return SourceCandidate(
            transport="sitemap",
            fetch_url=url,
            source_name=f"{host} sitemap index ({len(subs)} sub-sitemaps)",
            path_display=_path_display(url),
            last_updated=None,
            quality=_compute_quality(len(subs), None),
            preview_articles=preview,
            detected_via=detected_via,
        )
    return None


def _fetch_and_build_sitemap(
    url: str, detected_via: str, max_preview: int
) -> SourceCandidate | None:
    try:
        status, body, _, _stage = fetch_bytes(url)
    except Exception:  # noqa: BLE001
        return None
    if status >= 400:
        return None
    return _build_sitemap_candidate(url, body, detected_via, max_preview)


def _dedup_candidates(candidates: list[SourceCandidate]) -> list[SourceCandidate]:
    """同一 fetch_url の dedup (trailing slash 違い等)。 health_score 高い方を残す。"""

    def _norm(u: str) -> str:
        parsed = urlparse(u)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"

    by_key: dict[tuple[str, str], SourceCandidate] = {}
    for c in candidates:
        key = (c.transport, _norm(c.fetch_url))
        existing = by_key.get(key)
        if existing is None or c.quality.health_score > existing.quality.health_score:
            by_key[key] = c
    # source_name (feed title) 一致 alias の dedup (msrc.microsoft.com 等)
    by_title: dict[tuple[str, str], SourceCandidate] = {}
    no_title: list[SourceCandidate] = []
    for c in by_key.values():
        if not c.source_name:
            no_title.append(c)
            continue
        title_key = (c.transport, c.source_name.strip().lower())
        existing = by_title.get(title_key)
        if existing is None or len(c.fetch_url) < len(existing.fetch_url):
            by_title[title_key] = c
    return list(by_title.values()) + no_title


def discover_sources(
    url: str, max_preview_per_candidate: int = 5
) -> tuple[
    list[SourceCandidate],
    list[str],
]:
    """unified discovery。 全 transport を並列 probe + uniform candidate を返す。

    Returns:
        (candidates, notes)
    """
    candidates: list[SourceCandidate] = []
    notes: list[str] = []

    # === Step A: 入力 URL 自身を fetch ===
    site_body = ""
    site_headers: dict[str, str] = {}
    site_status = 0
    try:
        site_status, site_body, site_headers, _stage = fetch_text(
            url, timeout=INTERACTIVE_HTTP_TIMEOUT
        )
    except SourceFetchError as e:
        # ネットワーク到達不可 (timeout / bot 対策 tarpit 等)。同一ホストの追加 path を
        # probe しても全て同様に失敗し数分ハングするため、明確なメッセージで即 fail。
        notes.append(str(e))
        return _dedup_candidates(candidates), notes
    except Exception as e:  # noqa: BLE001
        _log.warning("source_discovery_site_fetch_failed", url=url, error=str(e))
        notes.append("サイトの取得に失敗しました")

    # === Step B: 入力 URL 自身が feed か判定 ===
    if site_status > 0 and site_status < 400:
        kind = detect_kind(site_body, site_headers)
        if kind in ("rss", "atom"):
            c = _validate_and_build_feed_candidate(
                url,
                kind,
                site_body,
                "direct",
                max_preview_per_candidate,
            )
            if c is not None:
                candidates.append(c)
        elif kind == "sitemap":
            try:
                # detect_kind は text だが parse_sitemap_xml は bytes 期待
                _, body_bytes, _, _ = fetch_bytes(url)
                c = _build_sitemap_candidate(
                    url,
                    body_bytes,
                    "direct",
                    max_preview_per_candidate,
                )
                if c is not None:
                    candidates.append(c)
            except Exception:  # noqa: BLE001
                pass

    # 直接 feed/sitemap だった場合はここで返却 (それ以上 probe 不要)
    if candidates and candidates[0].detected_via == "direct":
        return _dedup_candidates(candidates), notes

    # === Step C: HTML alternate link 抽出 ===
    if site_body and site_status < 400:
        alt_links = _extract_alternate_links(site_body, url)
        for link_url, link_title, _kind in alt_links:
            c = _fetch_and_build_feed(link_url, "html_link", max_preview_per_candidate)
            if c is not None:
                if link_title and not c.source_name:
                    c.source_name = link_title
                candidates.append(c)

    # === Step D: category path probe (user URL 配下) ===
    parsed = urlparse(url)
    base_root = f"{parsed.scheme}://{parsed.netloc}"
    user_path = parsed.path.rstrip("/")
    if user_path:
        for path in CATEGORY_PATHS:
            c = _fetch_and_build_feed(
                base_root + user_path + path,
                "category_path",
                max_preview_per_candidate,
            )
            if c is not None:
                candidates.append(c)

    # === Step E: common path probe (root 直下) ===
    if not candidates:
        for path in COMMON_FEED_PATHS:
            c = _fetch_and_build_feed(
                base_root + path,
                "common_path",
                max_preview_per_candidate,
            )
            if c is not None:
                candidates.append(c)

    # === Step F: subdomain probe ===
    if not candidates:
        host = parsed.netloc
        base_host = host[4:] if host.startswith("www.") else host
        sub_first = base_host.split(".", 1)[0]
        if sub_first not in COMMON_FEED_SUBDOMAINS:
            for sub in COMMON_FEED_SUBDOMAINS:
                sub_url = f"{parsed.scheme}://{sub}.{base_host}"
                for path in ("", "/rss", "/feed", "/index.xml"):
                    c = _fetch_and_build_feed(
                        sub_url + path,
                        "subdomain",
                        max_preview_per_candidate,
                    )
                    if c is not None:
                        candidates.append(c)
                        break

    # === Step G: sitemap probe (parallel) ===
    sitemap_urls_to_try: list[str] = []
    # 1. user URL 自身 (root に近い)
    if user_path:
        sitemap_urls_to_try.append(base_root + user_path + "/sitemap.xml")
    # 2. common sitemap paths from root
    sitemap_urls_to_try.extend(base_root + p for p in COMMON_SITEMAP_PATHS)
    # 3. robots.txt 経由 sitemap declaration
    try:
        rstatus, rbody, _, _ = fetch_text(base_root + "/robots.txt")
        if rstatus < 400:
            for line in rbody.splitlines():
                if line.lower().startswith("sitemap:"):
                    sm_decl = line.split(":", 1)[1].strip()
                    if sm_decl not in sitemap_urls_to_try:
                        sitemap_urls_to_try.append(sm_decl)
    except Exception:  # noqa: BLE001
        pass

    seen_sm_urls: set[str] = set()
    for sm_url in sitemap_urls_to_try:
        if sm_url in seen_sm_urls:
            continue
        seen_sm_urls.add(sm_url)
        c = _fetch_and_build_sitemap(sm_url, "sitemap_probe", max_preview_per_candidate)
        if c is not None:
            candidates.append(c)
            break  # 同 site で複数 sitemap 列挙は冗長、 1 つ見つかれば十分

    # === Step H: dedup + rank ===
    candidates = _dedup_candidates(candidates)
    candidates.sort(key=lambda c: c.quality.health_score, reverse=True)

    if not candidates:
        notes.append(
            "RSS/Atom/sitemap 候補が見つかりませんでした。 "
            "HTML 解析モードで記事一覧の URL を指定すると、抽出ルールを自動で提案します。",
        )
    return candidates, notes


async def discover_sources_async(
    url: str, max_preview_per_candidate: int = 5
) -> tuple[
    list[SourceCandidate],
    list[str],
]:
    """sync 版を thread executor で非同期実行 (FastAPI endpoint からの呼び出し用)。"""
    return await asyncio.to_thread(discover_sources, url, max_preview_per_candidate)
