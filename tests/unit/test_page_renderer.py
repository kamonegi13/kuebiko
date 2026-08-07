"""page_renderer の SSRF route guard ロジックの unit test。

実際の Playwright 描画は integration (重い) のためここでは
request_allowed の判定ロジックのみ検証する。
"""

from unittest.mock import patch

from src.tools import page_renderer
from src.tools.page_renderer import request_allowed


def setup_function() -> None:
    # lru_cache が test 間で汚染しないようクリア
    page_renderer._host_is_public.cache_clear()


def test_blocks_non_http_subrequest() -> None:
    assert request_allowed("data:image/png;base64,xxxx") is False
    assert request_allowed("file:///etc/passwd") is False


def test_blocks_private_ip_subrequest() -> None:
    with patch(
        "src.tools.page_renderer.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("10.0.0.5", 0))],
    ):
        assert request_allowed("http://internal.svc/asset.js") is False


def test_allows_public_subrequest() -> None:
    with patch(
        "src.tools.page_renderer.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
    ):
        assert request_allowed("https://cdn.example.com/style.css") is True


def test_blocks_metadata_ip_literal() -> None:
    # IP literal は DNS 解決せず即判定
    assert request_allowed("http://169.254.169.254/latest/meta-data/") is False


def test_allows_public_ip_literal() -> None:
    assert request_allowed("https://8.8.8.8/") is True
