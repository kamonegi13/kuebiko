"""Grok のレポート本体 (チャット) の URL の判定 (依存の少ない共通部品、2026-10-29)。

source_router (取得) / filters (重複判定) / orchestrator (未完了の判定) の 3 か所で使う。
grok_convert に置くと source_router との循環 import になるためここに分けた。
"""

from __future__ import annotations

from urllib.parse import urlparse

#: Grok のレポート本体のページ。毎回新しい URL で、通知メールの未読が再試行の権利
_GROK_REPORT_PATH_PREFIXES = ("/c/", "/chat/", "/share/")


def is_grok_report_url(url: str) -> bool:
    """Grok のレポート本体 (チャット) の URL か。重複判定の対象から外すために使う。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host != "grok.com" and not host.endswith(".grok.com"):
        return False
    return (parsed.path or "").startswith(_GROK_REPORT_PATH_PREFIXES)
