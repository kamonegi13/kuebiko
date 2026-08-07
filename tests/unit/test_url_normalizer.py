"""src.tools.url_normalizer のテスト (Phase 3a)。"""

from __future__ import annotations

import pytest

from src.tools.url_normalizer import normalize_url, url_hash


class TestNormalizeUrl:
    def test_lowercases_scheme_and_host(self) -> None:
        assert normalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"

    def test_strips_fragment(self) -> None:
        assert normalize_url("https://example.com/p#anchor") == "https://example.com/p"

    def test_removes_tracking_params(self) -> None:
        url = "https://example.com/?utm_source=x&utm_medium=y&q=hello&fbclid=abc"
        assert normalize_url(url) == "https://example.com/?q=hello"

    def test_sorts_query_params(self) -> None:
        url = "https://example.com/?b=2&a=1&c=3"
        assert normalize_url(url) == "https://example.com/?a=1&b=2&c=3"

    def test_collapses_double_slashes(self) -> None:
        assert normalize_url("https://example.com//foo//bar") == "https://example.com/foo/bar"

    def test_strips_default_ports(self) -> None:
        assert normalize_url("https://example.com:443/p") == "https://example.com/p"
        assert normalize_url("http://example.com:80/p") == "http://example.com/p"
        # 非デフォルトは保持
        assert normalize_url("http://example.com:8080/p") == "http://example.com:8080/p"

    def test_strips_trailing_dot_in_host(self) -> None:
        assert normalize_url("https://example.com./p") == "https://example.com/p"

    def test_keeps_empty_path_as_slash(self) -> None:
        assert normalize_url("https://example.com") == "https://example.com/"

    def test_returns_input_for_invalid_input(self) -> None:
        assert normalize_url("") == ""
        assert normalize_url("not a url") == "not a url"

    def test_idempotent(self) -> None:
        url = "HTTPS://EXAMPLE.com:443/A//B?utm_source=x&z=1#f"
        once = normalize_url(url)
        twice = normalize_url(once)
        assert once == twice


class TestUrlHash:
    def test_same_url_produces_same_hash(self) -> None:
        a = url_hash("https://example.com/foo")
        b = url_hash("https://example.com/foo")
        assert a == b

    def test_normalized_variants_produce_same_hash(self) -> None:
        a = url_hash("HTTPS://Example.com/foo?utm_source=x")
        b = url_hash("https://example.com/foo")
        assert a == b

    def test_different_urls_produce_different_hashes(self) -> None:
        a = url_hash("https://example.com/foo")
        b = url_hash("https://example.com/bar")
        assert a != b

    def test_returns_32_hex_chars(self) -> None:
        h = url_hash("https://example.com/")
        assert len(h) == 32
        assert all(c in "0123456789abcdef" for c in h)

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/a",
            "https://example.com/path?key=value",
            "http://localhost:3000/",
        ],
    )
    def test_no_exception_for_valid_urls(self, url: str) -> None:
        url_hash(url)
