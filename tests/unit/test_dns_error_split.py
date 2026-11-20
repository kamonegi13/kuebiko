"""名前解決の一時失敗を SSRF の拒否と分ける (2026-11-20、11-19 監査 D2 P0-3b)。

名前解決の失敗が ``UnsafeUrlError`` → ``unsafe_url`` (再取得しない恒久の失敗) に潰れ、
ホスト側の通信断の数時間に当たった記事の本文が永久に feed 要約のままだった
(BleepingComputer 30 日で 53 件)。``HostResolutionError`` は ``UnsafeUrlError`` の子なので、
既存の ``except UnsafeUrlError`` (sources API・sitemap 等) はそのまま遮断し続ける。
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import httpx
import pytest

from src.tools.content_extractor import ContentExtractor
from src.tools.url_guard import HostResolutionError, UnsafeUrlError, assert_safe_public_url


def _extractor() -> ContentExtractor:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    return ContentExtractor(client=client, enable_playwright_fallback=False)


def test_resolution_failure_is_host_resolution_error() -> None:
    with (
        patch("src.tools.url_guard.socket.getaddrinfo", side_effect=socket.gaierror("x")),
        pytest.raises(HostResolutionError) as info,
    ):
        assert_safe_public_url("https://down.example.com/a")
    assert isinstance(info.value, UnsafeUrlError)  # 既存の遮断はそのまま効く


def test_private_resolution_stays_unsafe_not_host_error() -> None:
    private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]
    with (
        patch("src.tools.url_guard.socket.getaddrinfo", return_value=private),
        pytest.raises(UnsafeUrlError) as info,
    ):
        assert_safe_public_url("https://internal.example.com/a")
    assert not isinstance(info.value, HostResolutionError)


@pytest.mark.asyncio
async def test_extractor_reports_dns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DNS_ERROR_SPLIT", "1")
    with patch("src.tools.url_guard.socket.getaddrinfo", side_effect=socket.gaierror("x")):
        async with _extractor() as ex:
            result = await ex.extract("https://down.example.com/a")
    assert result.failure_reason == "dns_error"


@pytest.mark.asyncio
async def test_extractor_split_off_keeps_unsafe_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DNS_ERROR_SPLIT", "0")
    with patch("src.tools.url_guard.socket.getaddrinfo", side_effect=socket.gaierror("x")):
        async with _extractor() as ex:
            result = await ex.extract("https://down.example.com/a")
    assert result.failure_reason == "unsafe_url"


def test_dns_error_does_not_consume_refetch_attempts() -> None:
    from src.storage.repo_articles import DEFERRED_REFETCH_REASONS

    assert "dns_error" in DEFERRED_REFETCH_REASONS
