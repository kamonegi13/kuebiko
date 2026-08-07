"""url_guard の SSRF 防御ロジックの unit test。

ユーザ指定 URL をサーバが fetch する proxy 機能のため、private/loopback/
link-local/metadata IP への到達を起動段階で弾くことを保証する (CLAUDE.md §4)。
"""

from unittest.mock import patch

import pytest

from src.tools.url_guard import UnsafeUrlError, assert_safe_public_url


def _fake_resolve(ip: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """getaddrinfo の戻り値を 1 IP に固定する patch helper。"""
    return [(2, 1, 6, "", (ip, 0))]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "gopher://example.com",
        "//no-scheme.example.com",
        "javascript:alert(1)",
    ],
)
def test_rejects_non_http_scheme(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        assert_safe_public_url(url)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private A
        "172.16.0.1",  # private B
        "192.168.1.1",  # private C
        "169.254.169.254",  # link-local / cloud metadata
        "0.0.0.0",  # unspecified
        "::1",  # loopback v6
        "fd00::1",  # ULA v6
        "fe80::1",  # link-local v6
    ],
)
def test_rejects_private_and_metadata_ips(ip: str) -> None:
    with (
        patch("src.tools.url_guard.socket.getaddrinfo", return_value=_fake_resolve(ip)),
        pytest.raises(UnsafeUrlError),
    ):
        assert_safe_public_url("https://malicious.example.com/")


def test_rejects_when_any_resolved_ip_is_private() -> None:
    # public + private が混在 → 拒否 (DNS rebinding 対策)
    mixed = [(2, 1, 6, "", ("93.184.216.34", 0)), (2, 1, 6, "", ("10.0.0.1", 0))]
    with (
        patch("src.tools.url_guard.socket.getaddrinfo", return_value=mixed),
        pytest.raises(UnsafeUrlError),
    ):
        assert_safe_public_url("https://example.com/")


def test_allows_public_url() -> None:
    with patch(
        "src.tools.url_guard.socket.getaddrinfo",
        return_value=_fake_resolve("93.184.216.34"),
    ):
        # 例外が出なければ OK
        assert_safe_public_url("https://example.com/en/analysis")


def test_rejects_missing_host() -> None:
    with pytest.raises(UnsafeUrlError):
        assert_safe_public_url("https:///path-only")


def test_is_public_ip_helper() -> None:
    from src.tools.url_guard import is_public_ip

    assert is_public_ip("8.8.8.8") is True
    assert is_public_ip("10.0.0.1") is False
    assert is_public_ip("169.254.169.254") is False
    assert is_public_ip("not-an-ip") is False


# ---------- redirect guard (M1: 30x で private へ誘導される SSRF を塞ぐ) ----------


class _FakeURL:
    def __init__(self, url: str) -> None:
        self._url = url

    def join(self, location: str) -> str:
        # 絶対 URL の Location を想定 (テストでは相対解決まで踏み込まない)
        return location


class _FakeResponse:
    def __init__(self, location: str | None, *, is_redirect: bool = True) -> None:
        self.is_redirect = is_redirect
        self.headers = {"location": location} if location else {}
        self.url = _FakeURL("https://safe.example.com/start")


def test_redirect_to_private_is_blocked() -> None:
    from src.tools.url_guard import _check_redirect

    resp = _FakeResponse("http://169.254.169.254/latest/meta-data/")
    with (
        patch(
            "src.tools.url_guard.socket.getaddrinfo",
            return_value=_fake_resolve("169.254.169.254"),
        ),
        pytest.raises(UnsafeUrlError),
    ):
        _check_redirect(resp)


def test_redirect_to_public_is_allowed() -> None:
    from src.tools.url_guard import _check_redirect

    resp = _FakeResponse("https://other.example.com/next")
    with patch(
        "src.tools.url_guard.socket.getaddrinfo",
        return_value=_fake_resolve("93.184.216.34"),
    ):
        _check_redirect(resp)  # 例外が出なければ OK


def test_non_redirect_response_skipped() -> None:
    from src.tools.url_guard import _check_redirect

    # is_redirect=False は Location を見ずに即 return (DNS 解決もしない)
    _check_redirect(_FakeResponse(None, is_redirect=False))
