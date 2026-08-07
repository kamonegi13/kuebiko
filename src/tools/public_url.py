"""外部公開 URL の解決 (Discord からの Web 誘導リンク用)。

Discord 投稿に載せる Web リンクの base URL を決める。優先順:

1. ``data/.mobile_tunnel_hostname`` ファイル — named tunnel の public hostname。
   **UI から設定でき、設定するだけで quick tunnel から自動移行する** (2026-07-30, C2 設計。
   契約の SSoT は :mod:`src.tools.mobile_tunnel_files`)。
2. ``CLOUDFLARE_TUNNEL_HOSTNAME`` env — 後方互換 fallback (旧 .env / compose 経路)。
3. ``data/.mobile_tunnel_url`` — quick tunnel の現 URL (tunnel sidecar が
   検出して書き込む flag file 契約、docker-compose.yml 参照)。tunnel 再作成で URL が
   変わるため過去投稿のリンクは切れうる (許容済み: 新しい投稿は常に現 URL を載せる)。
4. いずれも無ければ ``None`` — 呼び出し側はリンク無しの文言に fallback する。
"""

from __future__ import annotations

import os
from pathlib import Path

# URL file 定数の SSoT は mobile_tunnel_files (専用サブディレクトリ配下)。ui/api/pages.py も
# ここ経由で参照する。
from src.tools.mobile_tunnel_files import URL_FILE as MOBILE_TUNNEL_URL_FILE
from src.tools.mobile_tunnel_files import read_hostname

_NAMED_TUNNEL_ENV = "CLOUDFLARE_TUNNEL_HOSTNAME"


def resolve_public_base_url(*, url_file: Path = MOBILE_TUNNEL_URL_FILE) -> str | None:
    """外部到達可能な base URL (末尾 ``/`` なし) を返す。解決できなければ None。"""
    # 1. data/ ファイル (UI 管理の named tunnel hostname) → 2. env fallback
    hostname = read_hostname() or os.environ.get(_NAMED_TUNNEL_ENV, "").strip()
    if hostname:
        host = hostname.removeprefix("https://").removeprefix("http://").strip("/")
        if host:
            return f"https://{host}"
    try:
        if url_file.exists():
            content = url_file.read_text(encoding="utf-8").strip()
            if content.startswith(("https://", "http://")):
                return content.rstrip("/")
    except OSError:
        return None
    return None
