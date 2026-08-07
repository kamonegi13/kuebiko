"""ホスト常駐 watchdog との受け渡しファイル契約 (SSoT、2026-08-02)。

`mobile_tunnel_files` と同じ作法: **アプリ (コンテナ内) とホスト側プロセスは
`data/` 配下のファイルで疎結合に会話する**。UI はフラグを書くだけ、ホスト側は
毎回それを読むだけで、双方が互いのプロセスを直接操作しない。

なぜこの形か:
- watchdog は**コンテナランタイム自身を監視・修復する**ため、監視対象の内側 (コンテナ)
  には置けない。ホスト常駐が必然。
- 一方 UI はコンテナ内で動くのでホストの launchctl を操作できない。よって
  **導入/削除はターミナル (一度きり)、有効/無効は UI から real-time** に分担する。
- 状態とログを `data/` (コンテナへマウント済) に置くことで、**UI から watchdog の
  生死と復旧履歴が見える**。監視の活動が見えないのは、このツールがずっと直してきた
  「沈黙の意味を決められない」状態そのものなので避ける。

macOS 固有の事象 (OrbStack VM の sleep/wake 失敗) が対象なので、Linux サーバへ移す
場合はインストールしなければよい (実行時フットプリントはゼロ)。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DATA_DIR = Path(os.environ.get("HOST_WATCHDOG_DATA_DIR", "/app/data/host_watchdog"))

ENABLED_FLAG_FILE = _DATA_DIR / ".orbstack_watchdog_enabled"
STATE_FILE = _DATA_DIR / "state.json"
LOG_FILE = _DATA_DIR / "watchdog.log"

_MAX_LOG_TAIL_LINES = 40


def is_enabled(flag_file: Path | None = None) -> bool:
    """watchdog が有効か (フラグファイルの存在で表す。tunnel と同じ契約)。"""
    return (flag_file or ENABLED_FLAG_FILE).exists()


def set_enabled(enabled: bool, flag_file: Path | None = None) -> None:
    """有効/無効を切り替える (UI から呼ぶ。ホスト側は次回起動時に読む)。"""
    path = flag_file or ENABLED_FLAG_FILE
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    elif path.exists():
        path.unlink()


def read_state(state_file: Path | None = None) -> dict[str, Any]:
    """ホスト側が書いた状態を読む (未導入 / 未実行なら空 dict)。"""
    path = state_file or STATE_FILE
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data


def read_log_tail(limit: int = _MAX_LOG_TAIL_LINES, log_file: Path | None = None) -> list[str]:
    """直近のログ行 (新しい順)。復旧履歴の可視化に使う。"""
    path = log_file or LOG_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return list(reversed(lines[-limit:]))
