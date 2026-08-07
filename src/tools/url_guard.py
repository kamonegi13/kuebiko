"""SSRF guard: ユーザ指定 URL をサーバ側で fetch / 描画する前の安全性検証。

source wizard の visual picker proxy がユーザ入力 URL を取得するため、
private / loopback / link-local (cloud metadata) / multicast へのアクセスを
起動段階で拒否する (CLAUDE.md §4 セキュリティ要件)。
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeUrlError(ValueError):
    """SSRF guard が拒否した URL。"""


def is_public_ip(ip: str) -> bool:
    """IP 文字列が外部到達可能な public アドレスか判定する。

    private / loopback / link-local / multicast / unspecified / reserved は False。
    パースできない文字列も False。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
    )


def _resolve_ips(host: str) -> list[str]:
    """host を全 IP に解決する。解決失敗は UnsafeUrlError。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeUrlError(f"ホスト名を解決できません: {host}") from e
    return [str(info[4][0]) for info in infos]


def assert_safe_public_url(url: str) -> None:
    """url が public な http(s) リソースを指すことを検証する。

    違反時は ``UnsafeUrlError``。scheme / host の有無 / 全解決 IP が public かを確認。
    解決 IP が 1 つでも private 等なら拒否 (DNS rebinding 対策)。
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"許可されない scheme: {parsed.scheme!r} (http/https のみ)")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL に host がありません")

    ips = _resolve_ips(host)
    if not ips:
        raise UnsafeUrlError(f"ホスト名を解決できません: {host}")
    for ip in ips:
        if not is_public_ip(ip):
            raise UnsafeUrlError(
                f"private / loopback / metadata アドレスへのアクセスは禁止: {host} -> {ip}",
            )


# ---------- redirect guard (follow_redirects=True の各 hop を再検証) ----------
#
# 初回 URL が public でも、悪性サーバが 30x で private/loopback/metadata へ誘導する
# SSRF (TOCTOU) が成立する。httpx の response event hook で各 redirect hop の Location を
# 検証して塞ぐ。同期 client は同期 hook、非同期 client は async hook が必要なので両方提供する。


def _check_redirect(response: Any) -> None:
    """redirect 応答の Location が public か検証する (sync/async hook 共通本体)。"""
    if not getattr(response, "is_redirect", False):
        return
    location = response.headers.get("location")
    if not location:
        return
    # 相対 redirect は元 request URL を基準に絶対化してから検証する。
    assert_safe_public_url(str(response.url.join(location)))


def redirect_guard_hooks() -> dict[str, list[Any]]:
    """同期 ``httpx.Client`` 用の ``event_hooks``。各 redirect hop で SSRF 再検証する。"""
    return {"response": [_check_redirect]}


def redirect_guard_hooks_async() -> dict[str, list[Any]]:
    """非同期 ``httpx.AsyncClient`` 用の ``event_hooks`` (hook は coroutine 必須)。"""

    async def _hook(response: Any) -> None:
        _check_redirect(response)

    return {"response": [_hook]}
