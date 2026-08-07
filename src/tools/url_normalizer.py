"""URL 正規化 (Phase 3: 重複排除の前段)。

同一 URL の細かい揺らぎを吸収し、SHA-256 で安定したフィンガープリントを返す。

正規化ルール:
- スキーム / ホストを小文字化
- ホスト末尾のドットを削除
- パスの末尾スラッシュは保持しつつ重複スラッシュを除去
- フラグメント (#...) を削除
- 既知のトラッキングクエリ (utm_*, fbclid, gclid 等) を削除
- 残ったクエリは key でソート
- ポートはデフォルト (http=80, https=443) なら省略
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 一般的なアフィリエイト/トラッキングパラメータ。完全一致で削除する。
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "utm_id",
        "utm_name",
        "fbclid",
        "gclid",
        "yclid",
        "dclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "ref",
        "ref_src",
        "ref_url",
        "share_source",
        "trk",
        "vero_id",
    },
)

_DEFAULT_PORTS = {"http": 80, "https": 443}
_MULTI_SLASH_RE = re.compile(r"/{2,}")


def normalize_url(url: str) -> str:
    """URL を正規化された形式の文字列で返す。

    入力が空文字や不正な形式 (スキームなし等) の場合はそのまま返す。
    """
    if not url:
        return url

    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return url.strip()

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()

    # ホスト末尾のドット削除
    if "@" in netloc:
        # userinfo は保持 (実用ではほぼ無いが念のため)
        userinfo, _, host_part = netloc.rpartition("@")
        host_part = _strip_default_port(host_part, scheme).rstrip(".")
        netloc = f"{userinfo}@{host_part}"
    else:
        netloc = _strip_default_port(netloc, scheme).rstrip(".")

    # パスの重複スラッシュを除去
    path = _MULTI_SLASH_RE.sub("/", parts.path)
    if not path:
        path = "/"

    # クエリ正規化 (トラッキング系除去 + key ソート)
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in _TRACKING_PARAMS
    ]
    query_pairs.sort()
    query = urlencode(query_pairs)

    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    """正規化 URL の SHA-256 (16 文字 = 64 bit prefix) を返す。

    完全一致重複排除のためのキー。衝突確率は実用上無視できる。
    """
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _strip_default_port(host: str, scheme: str) -> str:
    """``host:port`` の port がスキームの既定値ならポート部分を削除する。"""
    if ":" not in host:
        return host
    name, _, port = host.rpartition(":")
    if port.isdigit() and _DEFAULT_PORTS.get(scheme) == int(port):
        return name
    return host
