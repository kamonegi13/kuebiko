"""NICTER 観測レポート watcher (Phase 5T-I)。

NICT サイバーセキュリティ研究室による darknet 観測の年次総合レポート。
日本国内のサイバー攻撃トレンド (国別アクセス元、宛先ポート、マルウェア
配布傾向) を分析。Japan 1 次データのユニークソース。

構造:
    - https://www.nicter.jp/report に年次 PDF URL がリスト化されている
    - PDF URL pattern: https://csl.nict.go.jp/report/NICTER_report_YYYY.pdf
    - 更新頻度: 年 1 回 (毎年 2 月頃に前年版が公開)
    - sitemap も RSS も無いため HTML 直接 scrape

設計判断:
    - PDF は trafilatura で本文抽出できないため、Article.summary_html に
      静的説明 (200+ 文字) を埋め込んで LLM triage が成立するようにする
    - 年 1 回しか発火しない watcher だが、CTI 担当の標準参照ソースなので
      公開直後に Discord に通知される価値がある
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import httpx

from src.logging_config import get_logger
from src.tools.article_model import Article
from src.tools.user_agent import browser_user_agent
from src.watchers.sitemap_base import WatcherState

_log = get_logger(__name__)

WATCHER_NAME = "nicter"
REPORT_INDEX_URL = "https://www.nicter.jp/report"
STATE_FILE = Path("data/nicter_seen.json")
FEED_TITLE = "NICTER 観測レポート (NICT)"

# PDF URL pattern: https://csl.nict.go.jp/report/NICTER_report_YYYY.pdf
_PDF_PATTERN = re.compile(
    r"https://csl\.nict\.go\.jp/report/NICTER_report_(\d{4})\.pdf",
    re.IGNORECASE,
)

# CF / WAF ブロック回避のため browser UA を使う (単一ソース = user_agent.browser_user_agent、
# 自己修復ジョブが更新する現行版を呼出時に共有。2026-07-27)。

# PDF は trafilatura で読めないため、summary_html に静的説明を埋め込んで
# extract_min_length (=200) をクリアし、LLM triage が動くようにする。
_SUMMARY_TEMPLATE = (
    "NICTER (Network Incident analysis Center for Tactical Emergency Response) "
    "は NICT (情報通信研究機構) サイバーセキュリティ研究室が運営する大規模 "
    "darknet 観測プラットフォーム。本レポートは {year} 年の 1 年間に "
    "観測されたパケット数・国別アクセス元 (ロシア・中国・米国等の APT 起点)・"
    "宛先ポート・マルウェア配布傾向を統計分析した年次総合レポート (PDF)。"
    "日本国内へのサイバー攻撃トレンド評価における日本側 1 次データソースとして、"
    "CTI 担当者の標準参照資料。詳細は本文 PDF を参照。"
    "URL: {url}"
)


def _load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen_urls", []))
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("nicter_seen_load_failed", error=str(e))
        return set()


def _save_seen(urls: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"seen_urls": sorted(urls)}
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


async def _fetch_report_pdf_urls() -> list[tuple[str, str]]:
    """NICTER /report ページから PDF URL を抽出して ``(url, year)`` のリストを返す。

    重複排除 + URL 順 (新→旧) でソート。
    """
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": browser_user_agent()},
    ) as c:
        try:
            r = await c.get(REPORT_INDEX_URL)
            r.raise_for_status()
        except httpx.HTTPError as e:
            _log.warning("nicter_index_fetch_failed", error=str(e))
            return []

    seen_url: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _PDF_PATTERN.finditer(r.text):
        url = m.group(0)
        year = m.group(1)
        if url in seen_url:
            continue
        seen_url.add(url)
        out.append((url, year))
    # 新しい年順 (降順)
    out.sort(key=lambda t: int(t[1]), reverse=True)
    return out


def _to_article(pdf_url: str, year: str) -> Article:
    article_id = f"nicter:{hashlib.sha1(pdf_url.encode()).hexdigest()[:16]}"  # noqa: S324
    title = f"NICTER 観測レポート {year}"
    summary = _SUMMARY_TEMPLATE.format(year=year, url=pdf_url)
    # 公開日は不明 (年次レポートは 2 月頃) のため取得時刻で代替
    published = datetime.now().astimezone()
    return Article(
        id=article_id,
        title=title,
        url=pdf_url,
        summary_html=summary,
        author="NICT サイバーセキュリティ研究室",
        published=published,
        feed_title=FEED_TITLE,
        feed_url=REPORT_INDEX_URL,
    )


async def fetch_articles(max_count: int = 5) -> list[Article]:
    """新規公開された NICTER 観測レポート PDF を ``Article`` として返す。

    年 1 回しか発火しないため、通常 run では 0 件返す。新版公開時のみ 1 件返す。
    """
    pdf_list = await _fetch_report_pdf_urls()
    if not pdf_list:
        _log.warning("nicter_no_pdf_found")
        return []

    seen = _load_seen()
    new_pdfs = [(url, year) for url, year in pdf_list if url not in seen]

    _log.info(
        "nicter_fetch",
        watcher=WATCHER_NAME,
        total_pdfs=len(pdf_list),
        seen=len(seen),
        new=len(new_pdfs),
    )

    if not new_pdfs:
        return []

    capped = new_pdfs[:max_count]
    # seen 更新 (fetch 時、SitemapWatcher と同設計)
    seen.update(u for u, _ in capped)
    _save_seen(seen)

    return [_to_article(url, year) for url, year in capped]


async def init_seen() -> int:
    """現状の全 PDF URL を seen 化 (初回 false-positive 防止)。"""
    pdf_list = await _fetch_report_pdf_urls()
    urls = {u for u, _ in pdf_list}
    _save_seen(urls)
    _log.info("nicter_init", marked_seen=len(urls))
    return len(urls)


def read_state() -> WatcherState:
    """状態スナップショット (UI 表示用、SitemapWatcher と同 schema)。"""
    if not STATE_FILE.exists():
        return WatcherState(
            name=WATCHER_NAME,
            seen_count=0,
            state_file_exists=False,
            last_modified=None,
            state_path=str(STATE_FILE),
        )
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        seen_count = len(data.get("seen_urls", []))
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("nicter_state_read_failed", error=str(e))
        seen_count = 0
    mtime = datetime.fromtimestamp(STATE_FILE.stat().st_mtime).astimezone()
    return WatcherState(
        name=WATCHER_NAME,
        seen_count=seen_count,
        state_file_exists=True,
        last_modified=mtime,
        state_path=str(STATE_FILE),
    )
