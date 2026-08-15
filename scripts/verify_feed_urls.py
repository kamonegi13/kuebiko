"""購読候補 feed URL の事前一括検証 (Phase 5Q-3)。

バッチ 1 で MSRC 403 / Volexity HTML / Project Zero 13MB malformed と
3 連続でハマった教訓から、subscribe 前に全 URL を以下の観点で検証する:

  1. HTTP status (200 必須、redirect は follow)
  2. Content-Type (xml / atom / rss を期待、HTML なら警告)
  3. XML validity (xmllint 相当を Python で実施)
  4. 最新エントリの published 日付 (stale 検出: 6 ヶ月以上前なら警告)
  5. エントリ数 (0 件なら警告)

出力は markdown 表形式。Pass/Warning/Fail で色分け。
NG が出たら subscribe しない、または watcher 化を検討。

使用方法:
    uv run python scripts/verify_feed_urls.py < urls.txt    # 標準入力から URL リスト
    uv run python scripts/verify_feed_urls.py url1 url2 ... # 引数で URL を渡す
"""

from __future__ import annotations

import asyncio
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

USER_AGENT = "kuebiko/1.0 (+feed-verifier)"
HTTP_TIMEOUT_SECONDS = 30.0
STALE_THRESHOLD_DAYS = 180  # 6 ヶ月以上更新なしを warning
MAX_BYTES_TO_DOWNLOAD = 20 * 1024 * 1024  # 20MB 上限 (Project Zero は 13MB)


@dataclass(frozen=True)
class VerifyResult:
    """1 URL の検証結果。"""

    url: str
    http_status: int | None
    content_type: str | None
    content_length: int | None
    is_xml: bool
    xml_valid: bool
    entry_count: int
    latest_date: datetime | None
    notes: list[str]
    verdict: str  # "PASS" / "WARN" / "FAIL"


# --- XML 構造から最新日付 + entry 数を抽出する ----------------------------

# RSS と Atom 両対応の published / updated タグ名
_ITEM_TAG_PATTERN = re.compile(
    r"<(?:item|entry)\b",
    re.IGNORECASE,
)
_DATE_TAG_PATTERN = re.compile(
    r"<(?:pubDate|published|updated|dc:date)[^>]*>([^<]+)</",
    re.IGNORECASE,
)


def _parse_date(text: str) -> datetime | None:
    """RFC 822 (RSS) と ISO 8601 (Atom) の両対応 date 解析。"""
    text = text.strip()
    # RFC 822: "Thu, 14 May 2026 07:04:50 Z" or "+0000"
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        pass
    # ISO 8601: "2026-05-13T11:18:22-07:00"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_dates_and_count(body: str) -> tuple[int, datetime | None]:
    """正規表現ベースで entry 数と最新 date を抽出する (XML 破損でも動く)。"""
    entries = _ITEM_TAG_PATTERN.findall(body)
    entry_count = len(entries)

    dates: list[datetime] = []
    for match in _DATE_TAG_PATTERN.finditer(body):
        dt = _parse_date(match.group(1))
        if dt is not None:
            dates.append(dt)

    latest = max(dates) if dates else None
    return entry_count, latest


def _xml_is_valid(body: str) -> bool:
    try:
        ET.fromstring(body)
        return True
    except ET.ParseError:
        return False


async def verify_url(url: str, client: httpx.AsyncClient) -> VerifyResult:
    notes: list[str] = []
    try:
        resp = await client.get(url, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True)
    except httpx.HTTPError as e:
        return VerifyResult(
            url=url,
            http_status=None,
            content_type=None,
            content_length=None,
            is_xml=False,
            xml_valid=False,
            entry_count=0,
            latest_date=None,
            notes=[f"HTTP error: {e}"],
            verdict="FAIL",
        )

    http_status = resp.status_code
    content_type = resp.headers.get("content-type", "")
    content_length: int | None = (
        int(resp.headers["content-length"]) if "content-length" in resp.headers else None
    )

    if http_status != 200:
        notes.append(f"non-200 status: {http_status}")
        return VerifyResult(
            url=url,
            http_status=http_status,
            content_type=content_type,
            content_length=content_length,
            is_xml=False,
            xml_valid=False,
            entry_count=0,
            latest_date=None,
            notes=notes,
            verdict="FAIL",
        )

    # 20MB 超は危険信号 (Project Zero パターン)
    body = resp.text[:MAX_BYTES_TO_DOWNLOAD]
    if content_length and content_length > MAX_BYTES_TO_DOWNLOAD:
        notes.append(f"巨大 feed ({content_length // (1024 * 1024)}MB)、parse 不能の懸念")

    ct_lower = content_type.lower()
    is_xml = "xml" in ct_lower or "atom" in ct_lower or "rss" in ct_lower
    if not is_xml:
        # text/html 等は誤 URL の可能性
        notes.append(f"non-XML content-type: {content_type}")

    xml_valid = _xml_is_valid(body)
    if not xml_valid:
        notes.append("XML parse error (CDATA 破損 / 巨大画像埋め込み等の懸念)")

    entry_count, latest = _extract_dates_and_count(body)
    if entry_count == 0:
        notes.append("entry が見つからない")

    if latest is not None:
        age = datetime.now(UTC) - latest.astimezone(UTC)
        if age > timedelta(days=STALE_THRESHOLD_DAYS):
            notes.append(f"最新エントリが {age.days} 日前 (stale)")

    # verdict 判定
    if not is_xml or entry_count == 0:
        verdict = "FAIL"
    elif not xml_valid or notes:
        verdict = "WARN"
    else:
        verdict = "PASS"

    return VerifyResult(
        url=url,
        http_status=http_status,
        content_type=content_type,
        content_length=content_length,
        is_xml=is_xml,
        xml_valid=xml_valid,
        entry_count=entry_count,
        latest_date=latest,
        notes=notes,
        verdict=verdict,
    )


async def verify_all(urls: list[str]) -> list[VerifyResult]:
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        return await asyncio.gather(*(verify_url(u, client) for u in urls))


def _fmt_result(r: VerifyResult) -> str:
    symbol = {"PASS": "✓", "WARN": "△", "FAIL": "✗"}[r.verdict]
    date_str = r.latest_date.strftime("%Y-%m-%d") if r.latest_date else "—"
    size_str = (
        f"{r.content_length // 1024}KB" if r.content_length and r.content_length < 10**7 else "—"
    )
    notes_str = "; ".join(r.notes) if r.notes else ""
    return (
        f"{symbol} {r.verdict:4s} | HTTP {r.http_status} | "
        f"entries {r.entry_count:3d} | latest {date_str} | size {size_str} | "
        f"{r.url}" + (f"\n         notes: {notes_str}" if notes_str else "")
    )


def main(argv: list[str]) -> int:
    if argv and argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0

    urls: list[str] = list(argv) if argv else [line.strip() for line in sys.stdin if line.strip()]
    if not urls:
        print("ERROR: URL を引数か stdin から渡してください", file=sys.stderr)
        return 1

    results = asyncio.run(verify_all(urls))

    pass_count = sum(1 for r in results if r.verdict == "PASS")
    warn_count = sum(1 for r in results if r.verdict == "WARN")
    fail_count = sum(1 for r in results if r.verdict == "FAIL")

    print(f"\n=== Feed verification: {len(results)} URLs ===")
    print(f"PASS={pass_count}  WARN={warn_count}  FAIL={fail_count}\n")
    for r in results:
        print(_fmt_result(r))

    # PASS + WARN は subscribe 候補、FAIL は subscribe しない
    return 0 if fail_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
