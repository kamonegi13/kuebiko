"""Phase A: 19 watcher site の feed 取得可能性を一括 probe。

各 site について:
  1. RSS feed の有無 (標準 path + HTML <link rel="alternate"> 抽出)
  2. sitemap.xml の有無
  3. Cloudflare anti-bot 検出
  4. 推奨 migration strategy (Inoreader RSS / Web Feed / 自前 watcher 維持)

出力: markdown レポート (stdout)。

Usage:
    uv run python scripts/probe_feed_availability.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
from lxml import etree, html

# ---- Watcher target inventory --------------------------------------------------------

# (name, site_root) の tuple list。src/watchers/*.py の URL から抽出。
WATCHERS: list[tuple[str, str]] = [
    # sitemap 型 watcher (15 件)
    ("acled", "https://acleddata.com/"),
    ("bsi_germany", "https://www.bsi.bund.de/"),
    ("ccdcoe", "https://ccdcoe.org/"),
    ("csa_singapore", "https://www.csa.gov.sg/"),
    ("enisa", "https://www.enisa.europa.eu/"),
    ("hudson", "https://www.hudson.org/"),
    ("ifri", "https://www.ifri.org/"),
    ("ipa", "https://www.ipa.go.jp/"),
    ("isw", "https://understandingwar.org/"),
    ("lookout", "https://www.lookout.com/"),
    ("ncsc_nz", "https://www.ncsc.govt.nz/"),
    ("nicter_blog", "https://blog.nicter.jp/"),
    ("nozomi", "https://www.nozominetworks.com/"),
    ("orf_india", "https://www.orfonline.org/"),
    ("wilson_center", "https://www.wilsoncenter.org/"),
    # HTML 型 watcher (4 件)
    ("nicter_report", "https://www.nicter.jp/"),
    ("lac_watch", "https://www.lac.co.jp/lacwatch/"),
    ("north_38", "https://www.38north.org/"),
    ("project_zero", "https://projectzero.google/"),
]

# RSS の標準 probe path。Atom と RSS の両方を含む。
STANDARD_FEED_PATHS: tuple[str, ...] = (
    "feed/",
    "rss/",
    "atom.xml",
    "feed.xml",
    "rss.xml",
    "index.xml",
    "feeds/all.atom.xml",
    "feeds/all.rss.xml",
)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# ---- Result dataclass ----------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    name: str
    site: str
    rss_urls: list[str] = field(default_factory=list)
    sitemap_url: str | None = None
    cloudflare_detected: bool = False
    cloudflare_blocking: bool = False  # 403 / 503 / challenge page
    rss_recent_items: list[str] = field(default_factory=list)  # title list (最大 5)
    error: str | None = None

    @property
    def has_rss(self) -> bool:
        return bool(self.rss_urls)

    @property
    def recommendation(self) -> str:
        if self.error:
            return f"⚠ 調査失敗: {self.error}"
        if self.has_rss:
            return f"✅ **Inoreader RSS 購読追加** ({len(self.rss_urls)} 候補)"
        if self.cloudflare_blocking:
            return "🚧 Cloudflare ブロック → 自前 watcher 維持"
        if self.sitemap_url:
            return "🔧 sitemap watcher 維持 (RSS 不在) or Web Feed 化検討"
        return "⚙ Web Feed pilot 候補 (RSS / sitemap 共になし)"


# ---- Probe logic ---------------------------------------------------------------------


def _detect_cloudflare(response: httpx.Response) -> tuple[bool, bool]:
    """response から Cloudflare の有無と blocking status を判定。

    Returns:
        (detected, blocking)
        - detected: cf-ray / server: cloudflare が出ているか
        - blocking: 403/503 + challenge page らしき body かどうか
    """
    headers = response.headers
    detected = (
        "cf-ray" in headers
        or headers.get("server", "").lower() == "cloudflare"
    )
    body_sample = response.text[:2000].lower() if response.content else ""
    is_challenge = any(
        marker in body_sample
        for marker in (
            "just a moment",
            "checking your browser",
            "cf-challenge",
            "cf_chl_",
            "attention required",
        )
    )
    blocking = detected and (response.status_code in (403, 503) or is_challenge)
    return detected, blocking


def _looks_like_rss(content: bytes) -> bool:
    """response body が RSS / Atom feed であるかを判定。"""
    if not content:
        return False
    head = content[:512].lower()
    return any(marker in head for marker in (b"<rss", b"<feed", b"<rdf:rdf"))


async def _fetch(
    client: httpx.AsyncClient, url: str, follow_redirects: bool = True
) -> httpx.Response | None:
    try:
        return await client.get(url, follow_redirects=follow_redirects)
    except (httpx.HTTPError, httpx.InvalidURL):
        return None


def _extract_rss_links_from_html(html_content: bytes, base_url: str) -> list[str]:
    """HTML の <link rel="alternate" type="application/rss+xml"> から RSS URL を抽出。"""
    try:
        tree = html.fromstring(html_content)
    except (etree.ParseError, ValueError):
        return []
    rss_links: list[str] = []
    for link in tree.xpath(
        '//link[@rel="alternate" and (contains(@type,"rss") or contains(@type,"atom"))]'
    ):
        href = link.get("href")
        if not href:
            continue
        rss_links.append(urljoin(base_url, href))
    return rss_links


def _extract_recent_titles(content: bytes, limit: int = 5) -> list[str]:
    """RSS / Atom body から最近の記事 title を最大 limit 件抽出。"""
    try:
        # RSS / Atom 両方を許容する xpath query
        parser = etree.XMLParser(recover=True, ns_clean=True)
        tree = etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError:
        return []
    if tree is None:
        return []
    titles: list[str] = []
    # RSS 2.0: //item/title, Atom: //entry/title
    for elem in tree.iter():
        tag = etree.QName(elem.tag).localname.lower() if elem.tag else ""
        if tag == "title" and elem.getparent() is not None:
            parent_tag = etree.QName(elem.getparent().tag).localname.lower()
            if parent_tag in ("item", "entry"):
                text = (elem.text or "").strip()
                if text:
                    titles.append(text)
                if len(titles) >= limit:
                    break
    return titles


async def probe_site(client: httpx.AsyncClient, name: str, site: str) -> ProbeResult:
    """1 site の feed 取得可能性を probe。"""
    # 1. HTML root を取得 (Cloudflare detection も兼ねる)
    root_resp = await _fetch(client, site)
    if root_resp is None:
        return ProbeResult(name=name, site=site, error="root URL 接続失敗")
    cf_detected, cf_blocking = _detect_cloudflare(root_resp)

    # 2. HTML から <link rel="alternate"> の RSS URL を抽出
    rss_candidates: list[str] = []
    if root_resp.status_code == 200 and not cf_blocking:
        rss_candidates.extend(_extract_rss_links_from_html(root_resp.content, site))

    # 3. 標準 feed path を probe
    for path in STANDARD_FEED_PATHS:
        candidate = urljoin(site, path)
        if candidate in rss_candidates:
            continue
        resp = await _fetch(client, candidate)
        if resp is None or resp.status_code != 200:
            continue
        if _looks_like_rss(resp.content):
            rss_candidates.append(candidate)

    # 4. 有効な RSS URL を確定 (重複除去 + RSS validity 再確認)
    valid_rss: list[str] = []
    recent_titles: list[str] = []
    seen: set[str] = set()
    for cand in rss_candidates:
        if cand in seen:
            continue
        seen.add(cand)
        resp = await _fetch(client, cand)
        if resp is None or resp.status_code != 200:
            continue
        if not _looks_like_rss(resp.content):
            continue
        valid_rss.append(cand)
        if not recent_titles:
            recent_titles = _extract_recent_titles(resp.content)

    # 5. sitemap.xml の確認
    sitemap_url: str | None = None
    for path in ("sitemap.xml", "sitemap_index.xml"):
        candidate = urljoin(site, path)
        resp = await _fetch(client, candidate)
        if resp is None or resp.status_code != 200:
            continue
        if b"<urlset" in resp.content[:512] or b"<sitemapindex" in resp.content[:512]:
            sitemap_url = candidate
            break

    return ProbeResult(
        name=name,
        site=site,
        rss_urls=valid_rss,
        sitemap_url=sitemap_url,
        cloudflare_detected=cf_detected,
        cloudflare_blocking=cf_blocking,
        rss_recent_items=recent_titles,
    )


# ---- Report rendering ----------------------------------------------------------------


def render_markdown(results: list[ProbeResult]) -> str:
    lines: list[str] = []
    lines.append("# Feed Availability Probe Report")
    lines.append("")
    lines.append(f"Probed {len(results)} watcher sites.")
    lines.append("")
    lines.append("## サマリー")
    lines.append("")
    rss_count = sum(1 for r in results if r.has_rss)
    cf_block_count = sum(1 for r in results if r.cloudflare_blocking)
    err_count = sum(1 for r in results if r.error)
    lines.append(f"- RSS 発見: **{rss_count}** / {len(results)} 件")
    lines.append(f"- Cloudflare blocking: {cf_block_count} 件")
    lines.append(f"- 調査失敗: {err_count} 件")
    lines.append("")
    lines.append("| watcher | RSS | sitemap | Cloudflare | 推奨 |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        rss_icon = "✅" if r.has_rss else "❌"
        sitemap_icon = "✅" if r.sitemap_url else "—"
        cf_icon = (
            "🚧 block"
            if r.cloudflare_blocking
            else ("⚠ detected" if r.cloudflare_detected else "—")
        )
        lines.append(
            f"| `{r.name}` | {rss_icon} | {sitemap_icon} | {cf_icon} | {r.recommendation} |"
        )
    lines.append("")
    lines.append("## 詳細")
    lines.append("")
    for r in results:
        lines.append(f"### `{r.name}` — {r.site}")
        lines.append("")
        if r.error:
            lines.append(f"- ⚠ Error: {r.error}")
        if r.has_rss:
            lines.append("- RSS URLs:")
            for u in r.rss_urls:
                lines.append(f"  - `{u}`")
            if r.rss_recent_items:
                lines.append("- Recent 5 items:")
                for t in r.rss_recent_items:
                    lines.append(f"  - {t}")
        else:
            lines.append("- RSS: 発見できず")
        lines.append(f"- sitemap: {r.sitemap_url or '—'}")
        cf_status = (
            "blocking"
            if r.cloudflare_blocking
            else ("detected" if r.cloudflare_detected else "absent")
        )
        lines.append(f"- Cloudflare: {cf_status}")
        lines.append(f"- 推奨: {r.recommendation}")
        lines.append("")
    return "\n".join(lines)


# ---- Main --------------------------------------------------------------------------


async def main() -> None:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml,*/*;q=0.9"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as client:
        tasks = [probe_site(client, name, site) for name, site in WATCHERS]
        results = await asyncio.gather(*tasks)
    print(render_markdown(results))


if __name__ == "__main__":
    asyncio.run(main())
