#!/usr/bin/env bash
# OrbStack 無応答の自動復旧 watchdog を macOS LaunchAgent 化する (2026-08-02)。
#
# 使い方 (リポジトリ直下で):
#   bash scripts/install_orbstack_watchdog_launchagent.sh            # 導入 / 更新
#   bash scripts/install_orbstack_watchdog_launchagent.sh --uninstall
#
# 位置づけ: CLAUDE.md §9 の「launchd 不使用」はスケジューラ用途 (APScheduler で代替)
# の話であり、**ホスト補助サービスの常駐化は例外**として LaunchAgent を正とする
# (claude-code-bridge と同じ扱い)。この watchdog は macOS 側の事象 (VM の
# sleep/wake 失敗) を相手にするため、コンテナ内には置けない。
set -euo pipefail

LABEL="io.kuebiko.orbstack-watchdog"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${REPO}/data/host_watchdog"
LOG="${DATA_DIR}/launchd.log"
SCRIPT="${REPO}/scripts/orbstack_watchdog.sh"
INTERVAL_SECONDS="${WATCHDOG_INTERVAL_SECONDS:-300}"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled: $LABEL"
  exit 0
fi

if [[ ! -f "$SCRIPT" ]]; then
  echo "watchdog スクリプトが見つかりません: $SCRIPT" >&2
  exit 1
fi
chmod +x "$SCRIPT"

mkdir -p "$HOME/Library/LaunchAgents" "$DATA_DIR"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${SCRIPT}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>KUEBIKO_REPO</key><string>${REPO}</string>
    <key>KUEBIKO_ENV_FILE</key><string>${REPO}/.env</string>
  </dict>
  <key>StartInterval</key><integer>${INTERVAL_SECONDS}</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>${LOG}</string>
  <key>StandardErrorPath</key><string>${LOG}</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: $LABEL (${INTERVAL_SECONDS}s 間隔)"
echo "  有効化は Web UI (ジョブ管理 → ホスト復旧 watchdog) のトグルから。"
echo "  ログ: ${DATA_DIR}/watchdog.log"
echo "  手動実行: bash $SCRIPT"
echo "  停止:     bash scripts/install_orbstack_watchdog_launchagent.sh --uninstall"
