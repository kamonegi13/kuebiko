#!/bin/bash
# claude-bridge sidecar entrypoint — claude CLI を永続 volume に自己インストール/更新。
#
# 設計 (2026-07-24): イメージは薄い殻 (python + bridge スクリプト) に保ち、CLI の実体は
# volume (/data) に置く。CLI 更新はここでの自己更新で完結し、イメージ再ビルドを要さない。
# 更新を止めて版固定したい場合は CLAUDE_BRIDGE_AUTOUPDATE=0。
set -u

export HOME=/data/claude-home
CLAUDE_BIN="$HOME/.local/bin/claude"
mkdir -p "$HOME"

if [ ! -x "$CLAUDE_BIN" ]; then
  echo "[entrypoint] claude CLI が未導入 — volume へインストールします"
  if curl -fsSL https://claude.ai/install.sh | bash; then
    echo "[entrypoint] インストール完了: $("$CLAUDE_BIN" --version 2>/dev/null || echo unknown)"
  else
    echo "[entrypoint] インストール失敗 (ネットワーク?) — bridge は CLI なしで起動し /health が ok:false を返します"
  fi
elif [ "${CLAUDE_BRIDGE_AUTOUPDATE:-1}" = "1" ]; then
  echo "[entrypoint] claude update (現行: $("$CLAUDE_BIN" --version 2>/dev/null || echo unknown))"
  "$CLAUDE_BIN" update || echo "[entrypoint] update 失敗 (既存バイナリで続行)"
fi

exec python /app/scripts/claude_code_bridge.py --host 0.0.0.0 --port 8010
