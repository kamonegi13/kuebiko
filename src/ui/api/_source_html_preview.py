"""Phase F: HTML scrape mode 用 preview。

user が「これが記事一覧 page」 と指定した URL に対して:
1. LLM (main model, think=False) で CSS selector を提案
2. selector を apply して sample article URL + title を抽出
3. SourceCandidate (transport=html_scraper) として返す

Phase E-2 の scrapers.py suggest_selectors と同等処理だが、
SourceCandidate uniform schema で返す点が異なる。
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel

from src.config_loader import load_app_config
from src.logging_config import get_logger
from src.ui.api._source_http import (
    INTERACTIVE_HTTP_TIMEOUT,
    SourceFetchError,
    describe_blocked_status,
    fetch_text,
)
from src.ui.api._source_models import PreviewArticle, Quality, SourceCandidate

_log = get_logger(__name__)

HTML_FOR_LLM_MAX = 12000


def _trim_html_for_llm(html: str) -> str:
    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
    # 巨大な nav/header/footer chrome を先に除去し、12KB budget を本文に当てる。
    # MERICS 等の CMS は記事リストが body 20KB 目以降に来て先頭固定 truncate では届かない。
    for chrome in ("header", "nav", "footer", "aside"):
        cleaned = re.sub(
            rf"<{chrome}[^>]*>.*?</{chrome}>",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
    # <main> / [role=main] があれば本文領域として優先抽出、無ければ <body>。
    region = (
        re.search(r"<main[^>]*>(.*?)</main>", cleaned, flags=re.DOTALL | re.IGNORECASE)
        or re.search(
            r"<([a-z]+)[^>]*\brole=[\"']main[\"'][^>]*>(.*?)</\1>",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        or re.search(r"<body[^>]*>(.*?)</body>", cleaned, flags=re.DOTALL | re.IGNORECASE)
    )
    body = region.groups()[-1] if region else cleaned
    body = re.sub(r"\s+", " ", body)
    if len(body) > HTML_FOR_LLM_MAX:
        body = body[:HTML_FOR_LLM_MAX] + "... [truncated]"
    return body


def _apply_selectors_html(
    html: str,
    link_selector: str,
    title_selector: str,
    page_url: str,
    max_preview: int,
    url_include_pattern: str = "",
) -> list[PreviewArticle]:
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise RuntimeError(f"beautifulsoup4 not available: {e}") from e
    from src.tools.link_title import is_better_title, resolve_link_title

    url_filter = None
    if url_include_pattern.strip():
        try:
            url_filter = re.compile(url_include_pattern)
        except re.error as e:
            raise RuntimeError(f"取り込む範囲の指定が不正です: {e}") from e
    soup = BeautifulSoup(html, "html.parser")
    # 同一 URL が複数回一致する (画像ラッパ a と見出し a) ため、URL をキーに保持して
    # **より良いタイトルが後から来たら差し替える** (先勝ちだと無題で固定される)。
    found: dict[str, str] = {}
    try:
        links = soup.select(link_selector)
    except Exception as e:  # noqa: BLE001
        _log.warning("source_html_preview_invalid_selector", selector=link_selector, error=str(e))
        raise RuntimeError("リンクの抽出ルールが不正です") from e
    for el in links:
        href = el.get("href")
        if not href:
            inner = el.find("a")
            if inner is not None:
                href = inner.get("href")
        if not href:
            continue
        full_url = urljoin(page_url, str(href))[:500]
        # 一覧ページのリンクには記事以外 (タグ / 対象者 / パンくず) が混ざる。
        # 取り込む範囲を指定されていれば、ここで落として **見えているもの = 入るもの** にする。
        if url_filter is not None and not url_filter.search(full_url):
            continue
        if full_url not in found and len(found) >= max_preview:
            continue
        title = resolve_link_title(el, title_selector, url=full_url)
        if full_url not in found or is_better_title(title, found[full_url], url=full_url):
            found[full_url] = title
    return [PreviewArticle(title=t or "(no title)", url=u) for u, t in found.items()]


async def preview_html_listing(
    listing_url: str,
    hint: str = "",
    max_preview: int = 5,
) -> SourceCandidate:
    """listing_url に対して LLM CSS selector を提案 + preview を返す。"""
    from src.tools.model_tiers import Step, build_llm_for

    status, html, headers, _stage = fetch_text(listing_url, timeout=INTERACTIVE_HTTP_TIMEOUT)
    if status >= 400:
        raise SourceFetchError(describe_blocked_status(status, html, headers, listing_url))
    if not html:
        raise RuntimeError("一覧ページの本文を取得できませんでした")

    snippet = _trim_html_for_llm(html)
    hint_section = f"\n## user hint\n{hint}\n" if hint.strip() else ""

    cfg = load_app_config()
    # selector 提案は構造化抽出タスク。インタラクティブ UI 経路なので fast ティア (26B MoE) を使う。
    llm = build_llm_for(Step.SELECTOR_PROPOSAL, cfg)

    prompt = (
        "あなたは HTML ページから記事 list を抽出する CSS selector を提案する任務です。\n\n"
        f"## 入力 page URL\n{listing_url}\n"
        f"{hint_section}\n"
        "## 入力 HTML 抜粋 (body、 script/style 除去済)\n"
        f"```html\n{snippet}\n```\n\n"
        "## task\n"
        "1. このページが記事一覧 (blog index / advisory list / news feed) かを判定\n"
        "2. 各記事への link を抽出する CSS selector を 1 つ提案 (BeautifulSoup .select() 互換)\n"
        "3. 各記事 element 内で title を抽出する relative CSS selector を提案 (空文字も可)\n"
        "4. 簡潔な日本語 rationale を 1-2 文で\n\n"
        "## 制約\n"
        "- article_link_selector は <a> element または <a> を含む element を返す selector\n"
        "- ex: ``article.post-card a.post-link``、 ``.entry-title a``、 ``ul.advisory-list li a``\n"
        "- selector は overly specific を避け、 class や semantic タグを優先\n"
        "- title_selector は article element 内で相対 path、 空可\n"
        "- pagination / category / footer link を含まないように\n\n"
        'JSON で {"article_link_selector": str, "title_selector": str, '
        '"rationale": str} を返してください。'
    )

    class _SelectorOut(BaseModel):
        article_link_selector: str
        title_selector: str = ""
        rationale: str = ""

    # think=False: thinking モードは構造化抽出で推論ブロックが膨張し
    # インタラクティブ応答が timeout する (gemma_4_thinking_breaks_digests と同根)。
    out = await llm.generate_structured(
        prompt,
        _SelectorOut,
        temperature=0.1,
        think=False,
    )

    return _build_candidate_from_selectors(
        html=html,
        listing_url=listing_url,
        article_link_selector=out.article_link_selector,
        title_selector=out.title_selector,
        rationale=out.rationale,
        detected_via="html_scrape_user_intent",
        max_preview=max_preview,
    )


def _path_hints(urls: list[str]) -> list[str]:
    """URL 群の第 1 パス区分を頻度順に返す (取り込む範囲の選択肢)。"""
    from collections import Counter

    counter: Counter[str] = Counter()
    for u in urls:
        seg = urlparse(u).path.strip("/").split("/")[0]
        if seg:
            counter[seg] += 1
    return [seg for seg, _n in counter.most_common(6)]


def _detect_source_name(html: str, listing_url: str) -> str:
    """<title> から source_name を推測。失敗時は netloc。"""
    source_name = urlparse(listing_url).netloc
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        page_title_el = soup.find("title")
        if page_title_el:
            page_title = page_title_el.get_text(" ", strip=True)
            if page_title and len(page_title) < 100:
                source_name = page_title
    except Exception:  # noqa: BLE001
        pass
    return source_name


def _build_candidate_from_selectors(
    *,
    html: str,
    listing_url: str,
    article_link_selector: str,
    title_selector: str,
    rationale: str,
    detected_via: str,
    max_preview: int,
    url_include_pattern: str = "",
) -> SourceCandidate:
    """html + 明示 selector から SourceCandidate を組み立てる (LLM 非依存)。"""
    preview = _apply_selectors_html(
        html,
        article_link_selector,
        title_selector,
        listing_url,
        max_preview,
        url_include_pattern,
    )
    # 絞り込みの選択肢: 抽出できた URL のパス区分 (記事とタグを分けるのに使う)。
    all_links = _apply_selectors_html(html, article_link_selector, title_selector, listing_url, 60)
    hints = _path_hints([a.url for a in all_links])
    health = 80 if len(preview) >= 5 else max(20, len(preview) * 15)
    return SourceCandidate(
        transport="html_scraper",
        fetch_url=listing_url,
        source_name=_detect_source_name(html, listing_url),
        path_display=urlparse(listing_url).path or "/",
        last_updated=None,
        quality=Quality(
            entries_count=len(preview),
            freshness_days=None,
            health_score=health,
        ),
        preview_articles=preview,
        detected_via=detected_via,
        article_link_selector=article_link_selector,
        title_selector=title_selector,
        rationale=rationale,
        url_include_pattern=url_include_pattern or None,
        path_hints=hints,
    )


async def preview_html_listing_explicit(
    listing_url: str,
    article_link_selector: str,
    title_selector: str = "",
    max_preview: int = 5,
    url_include_pattern: str = "",
) -> SourceCandidate:
    """ユーザがビジュアルピッカー / 手動入力で指定した selector で preview を返す。

    LLM を呼ばず、selector をそのまま全 DOM に適用する (決定的)。
    """
    if not article_link_selector.strip():
        raise RuntimeError("リンクの抽出ルールが空です")
    status, html, headers, _stage = fetch_text(listing_url, timeout=INTERACTIVE_HTTP_TIMEOUT)
    if status >= 400:
        raise SourceFetchError(describe_blocked_status(status, html, headers, listing_url))
    if not html:
        raise RuntimeError("一覧ページの本文を取得できませんでした")
    return _build_candidate_from_selectors(
        html=html,
        listing_url=listing_url,
        article_link_selector=article_link_selector,
        title_selector=title_selector,
        rationale="ユーザがビジュアルピッカー / 手動で指定した selector",
        detected_via="html_scrape_visual_picker",
        max_preview=max_preview,
        url_include_pattern=url_include_pattern,
    )
