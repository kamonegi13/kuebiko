#!/usr/bin/env bash
# claude-code-bridge を macOS LaunchAgent 化する (ログイン時自動起動 + KeepAlive)。
#
# 使い方 (リポジトリ直下で):
#   bash scripts/install_claude_bridge_launchagent.sh            # 導入 / 更新
#   bash scripts/install_claude_bridge_launchagent.sh --uninstall
#
# 位置づけ: CLAUDE.md §9 の「launchd 不使用」はスケジューラ用途 (APScheduler で代替) の
# 話であり、ホスト補助サービス (Ollama 相当) の常駐化はこの LaunchAgent を正とする (§10)。
set -euo pipefail

LABEL="com.cti.claude-code-bridge"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/claude-bridge.log"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled: $LABEL"
  exit 0
fi

if [[ ! -x "$UV_BIN" ]]; then
  echo "uv が見つかりません ($UV_BIN)" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${UV_BIN}</string>
    <string>run</string>
    <string>python</string>
    <string>scripts/claude_code_bridge.py</string>
  </array>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${LOG}</string>
  <key>StandardErrorPath</key><string>${LOG}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF

# 再読込 (既存があれば入れ替え)
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: $LABEL (log: $LOG)"
launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | grep -E "state|pid" | head -3 || true
