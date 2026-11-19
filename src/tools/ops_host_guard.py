"""公開面に運用画面のホスト名を出さない関門 (2026-11-20、11-19 監査 P0-1)。

匿名で読める公開面 (標準・アドバンスド) に、Access で守る運用画面のホスト名を
載せない (CLAUDE.md §12: 運用画面への導線は匿名公開面から出さない)。

- 実ドメインはリポジトリに書かない。禁止するホストは**実行時に**集める
  (tunnel の hostname ファイル・quick tunnel の URL ファイル・環境変数)。
- 止めるのは**実ホストとの完全一致だけ**。``ops.`` 始まりや quick tunnel の形で
  止めると、攻撃者の quick tunnel を IOC として書いた CTI 記事で書き出しが止まる。
- ファイルが在るのに読めないときは止める (関門が黙って空になるのを防ぐ)。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from src.tools import mobile_tunnel_files

ENV_HOST = "CLOUDFLARE_TUNNEL_HOSTNAME"

# ホスト名の一部 (英数・ハイフン・ドット) が前後に続くなら別のホスト
_HOST_CHAR = r"[A-Za-z0-9.\-]"


class OpsHostUnavailableError(RuntimeError):
    """運用ホストの置き場所が在るのに読めない。"""


def _host_of(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").lower()


def _read_file_host(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return _host_of(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise OpsHostUnavailableError(f"運用ホストのファイルを読めない: {path.name}") from e


def collect_ops_hosts(
    *, hostname_file: Path, url_file: Path, env_host: str | None
) -> tuple[str, ...]:
    """運用画面のホスト名を集める (小文字・重複なし・順序は hostname → url → env)。"""
    found = (
        _read_file_host(hostname_file),
        _read_file_host(url_file),
        _host_of(env_host or ""),
    )
    return tuple(dict.fromkeys(h for h in found if h))


def collect_runtime_ops_hosts() -> tuple[str, ...]:
    """既定の置き場所から集める (``MOBILE_TUNNEL_DATA_DIR`` で場所を差し替えられる)。"""
    return collect_ops_hosts(
        hostname_file=mobile_tunnel_files.HOSTNAME_FILE,
        url_file=mobile_tunnel_files.URL_FILE,
        env_host=os.environ.get(ENV_HOST),
    )


def find_ops_host_refs(text: str, hosts: tuple[str, ...]) -> tuple[str, ...]:
    """text に現れる運用ホストを返す (大文字小文字を区別しない、ホスト名の境界つき)。"""
    hits = []
    for host in hosts:
        pattern = rf"(?<!{_HOST_CHAR}){re.escape(host)}(?!{_HOST_CHAR}*[A-Za-z0-9])"
        if re.search(pattern, text, flags=re.IGNORECASE):
            hits.append(host)
    return tuple(hits)


__all__ = [
    "ENV_HOST",
    "OpsHostUnavailableError",
    "collect_ops_hosts",
    "collect_runtime_ops_hosts",
    "find_ops_host_refs",
]
