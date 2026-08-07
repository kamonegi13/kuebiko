"""UA ローテーション retry の単体テスト (2026-07-27)。

WAF が UA バージョン fingerprint でブロックする挙動 (実測: Chrome/120=403 / Chrome/126=200) の
救済。プライマリ UA で 403 のとき代替 UA で再試行し 200 を得ることを検証する。
"""

from __future__ import annotations

import httpx
import pytest

from src.tools.content_extractor import ContentExtractor, _rotation_uas

_ARTICLE_HTML = (
    "<html lang='en'><head><title>t</title></head><body><article>"
    + ("Iranian APT actors tracked as CyberAv3ngers exploited PLCs. " * 20)
    + "</article></body></html>"
)


def _blocking_transport(*, ok_uas: set[str]) -> httpx.MockTransport:
    """許可 UA のみ 200、それ以外は 403 を返す MockTransport (WAF の UA ブロック模倣)。"""

    def handler(request: httpx.Request) -> httpx.Response:
        ua = request.headers.get("User-Agent", "")
        if ua in ok_uas:
            return httpx.Response(200, text=_ARTICLE_HTML)
        return httpx.Response(403, text="blocked")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_ua_rotation_recovers_from_403() -> None:
    """プライマリ UA が 403 でも、ローテ内の代替 UA が 200 なら全文抽出に成功する。"""
    # 代替 UA の 1 つ (Firefox) のみ許可 = プライマリは弾かれる状況を作る
    firefox_ua = _rotation_uas("Chrome/OLD")[1]
    transport = _blocking_transport(ok_uas={firefox_ua})
    client = httpx.AsyncClient(transport=transport, headers={"User-Agent": "Chrome/OLD"})
    ex = ContentExtractor(client=client, enable_playwright_fallback=False, user_agent="Chrome/OLD")

    result = await ex.extract("https://example.com/blocked-by-old-ua")

    assert result.success is True
    assert "CyberAv3ngers".lower() in result.text.lower()
    await client.aclose()


@pytest.mark.asyncio
async def test_all_uas_blocked_reports_http_error() -> None:
    """全 UA が 403 なら (playwright 無効時) http_error_403 で失敗を返す (無音にしない)。"""
    transport = _blocking_transport(ok_uas=set())
    client = httpx.AsyncClient(transport=transport, headers={"User-Agent": "Chrome/OLD"})
    ex = ContentExtractor(client=client, enable_playwright_fallback=False, user_agent="Chrome/OLD")

    result = await ex.extract("https://example.com/always-blocked")

    assert result.success is False
    assert result.failure_reason == "http_error_403"
    await client.aclose()


@pytest.mark.asyncio
async def test_no_retry_when_primary_succeeds() -> None:
    """プライマリ UA で 200 なら再試行しない (200 即返し)。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("User-Agent", ""))
        return httpx.Response(200, text=_ARTICLE_HTML)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": "Chrome/OK"}
    )
    ex = ContentExtractor(client=client, enable_playwright_fallback=False, user_agent="Chrome/OK")

    result = await ex.extract("https://example.com/ok")

    assert result.success is True
    assert len(calls) == 1  # 再試行していない
    await client.aclose()
