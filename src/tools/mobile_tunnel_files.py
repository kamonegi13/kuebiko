"""tunnel sidecar との data/ ファイル契約 (SSoT)。

app コンテナ (uid 1001) と tunnel コンテナ (root) が ``/app/data`` 共有
bind mount 越しに交換するファイル群のパスと read/write ヘルパをここに集約する。

named tunnel の ``token`` / ``hostname`` を **UI から管理**するため、従来 ``.env`` +
compose env だった値を data/ ファイルに移した (2026-07-30, C2 設計)。これにより
UI 保存 → launcher がファイル mtime を検知して自動再起動 → 再デプロイ不要で反映、が成立する。

セキュリティ規約 (CLAUDE.md §4/§12):
- ``token`` は秘密情報。ファイルは **0600** で書く (owner=app uid 1001、tunnel は root で読む)。
  最前線 (インターネット公開) の tunnel コンテナには **この専用サブディレクトリ
  (``data/mobile_tunnel``) だけ** を bind mount する (``.env`` も ``./data`` 全体も渡さない
  = least-privilege、M1 対応済 2026-07-30)。app コンテナは ``./data`` 全体を mount するので
  この subdir も見える。
- ``token`` の生値は API で返さない (:func:`is_token_set` の boolean と masked のみ)。この
  「token は full instance から出さない」不変条件は **アプリのコード規律** (raw token を返す
  エンドポイントを作らない) で担保する。readonly instance は同一 uid で data/ を共有し 0600 でも
  読めるため、**ファイル権限が full/readonly の境界を作るわけではない** (M2)。
- ``token`` をログに出さない。
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

# tunnel 契約ファイルの専用ディレクトリ。tunnel コンテナはここ **だけ** を bind mount する
# (least-privilege)。app コンテナは ./data 全体を mount するのでこの subdir も見える。
# テストは MOBILE_TUNNEL_DATA_DIR 環境変数か、モジュール定数の monkeypatch で差し替える。
_DATA_DIR = Path(os.environ.get("MOBILE_TUNNEL_DATA_DIR", "/app/data/mobile_tunnel"))

ENABLED_FLAG_FILE = _DATA_DIR / ".mobile_tunnel_enabled"
URL_FILE = _DATA_DIR / ".mobile_tunnel_url"
TOKEN_FILE = _DATA_DIR / ".mobile_tunnel_token"
HOSTNAME_FILE = _DATA_DIR / ".mobile_tunnel_hostname"

_TOKEN_FILE_MODE = 0o600  # 秘密: owner のみ
_HOSTNAME_FILE_MODE = 0o644  # 非秘密 (公開ホスト名)


def _read_text(path: Path) -> str | None:
    """ファイル内容を strip して返す。空・不在・読取失敗は None。"""
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            return content or None
    except OSError:
        return None
    return None


def _normalize_hostname(value: str) -> str:
    """scheme / 末尾スラッシュを除いた素のホスト名にする。"""
    return value.strip().removeprefix("https://").removeprefix("http://").strip("/")


def read_hostname(hostname_file: Path | None = None) -> str | None:
    """設定済みの public hostname (scheme なし) を返す。未設定は None。"""
    host = _read_text(hostname_file or HOSTNAME_FILE)
    if not host:
        return None
    return _normalize_hostname(host) or None


def is_token_set(token_file: Path | None = None) -> bool:
    """named tunnel の token が設定済みかどうか (生値は返さない)。"""
    return _read_text(token_file or TOKEN_FILE) is not None


def is_enabled(flag_file: Path | None = None) -> bool:
    """supervisor が cloudflared を起動すべき状態か (flag file の有無)。"""
    return (flag_file or ENABLED_FLAG_FILE).exists()


def mode(token_file: Path | None = None, flag_file: Path | None = None) -> str:
    """公開モードを返す: ``"named"`` | ``"quick"`` | ``"off"``。

    - off: tunnel 無効 (flag file なし)
    - named: token 設定済み → 固定 URL の named tunnel
    - quick: token 未設定 → account-less quick tunnel (URL 変動)
    """
    if not is_enabled(flag_file):
        return "off"
    return "named" if is_token_set(token_file) else "quick"


def _atomic_write(path: Path, content: str, *, mode_bits: int) -> None:
    """同一ディレクトリに tmp を作り os.replace で atomic 置換する (権限 = mode_bits)。

    tmp を同一ディレクトリに置くのは os.replace を同一ファイルシステム内 rename に
    保つため (cross-device rename 回避)。失敗時は tmp を掃除する。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(tmp, mode_bits)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_token(value: str, token_file: Path | None = None) -> None:
    """named tunnel の token を **0600** で書く。空文字は clear と同義。"""
    target = token_file or TOKEN_FILE
    stripped = value.strip()
    if not stripped:
        clear_token(target)
        return
    _atomic_write(target, stripped, mode_bits=_TOKEN_FILE_MODE)


def write_hostname(value: str, hostname_file: Path | None = None) -> None:
    """public hostname を scheme 除去して書く。空文字は clear と同義。"""
    target = hostname_file or HOSTNAME_FILE
    normalized = _normalize_hostname(value)
    if not normalized:
        clear_hostname(target)
        return
    _atomic_write(target, normalized, mode_bits=_HOSTNAME_FILE_MODE)


def clear_token(token_file: Path | None = None) -> None:
    """token ファイルを削除する (named → quick に戻す)。不在は無視。"""
    with contextlib.suppress(FileNotFoundError):
        (token_file or TOKEN_FILE).unlink()


def clear_hostname(hostname_file: Path | None = None) -> None:
    """hostname ファイルを削除する。不在は無視。"""
    with contextlib.suppress(FileNotFoundError):
        (hostname_file or HOSTNAME_FILE).unlink()
