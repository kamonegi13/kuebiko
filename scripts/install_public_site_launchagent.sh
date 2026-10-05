#!/usr/bin/env bash
# 公開サイト (標準面) の配信を macOS LaunchAgent 化する (毎時・変更があったときだけ
# 上げる。scripts/deploy_site.sh public を呼び、アドバンスドと合わせて 1 回で配信する)。
#
# 使い方 (リポジトリ直下で):
#   bash scripts/install_public_site_launchagent.sh            # 導入 / 更新
#   bash scripts/install_public_site_launchagent.sh --uninstall
#
# なぜホスト側か: 配信は `docker exec` (書き出し) と `npx wrangler` (アップロード) を
# 使うため、アプリのコンテナからは実行できない。CLAUDE.md §9 の「launchd 不使用」は
# **パイプラインのスケジューラ**用途の話であり (それは APScheduler が持つ)、ホスト補助の
# 常駐化は claude-code-bridge と同じくこの LaunchAgent を正とする。
#
# 配信スクリプト自身が **中身のハッシュを比較して、変わっていなければ何もしない**。
# 実測 (2026-08-26) で 1 日のうち 8-10 時間は生成がゼロなので、毎時起動しても
# 実配信はその分だけ減る。
set -euo pipefail

LABEL="com.cti.kuebiko-public-site"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/kuebiko-public-site.log"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled: $LABEL"
  exit 0
fi

NODE_BIN="$(dirname "$(command -v npx || true)")"
if [[ -z "$NODE_BIN" || ! -x "$NODE_BIN/npx" ]]; then
  echo "npx が見つかりません。node を入れてから実行してください" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat >| "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${REPO}/scripts/deploy_site.sh</string>
    <string>public</string>
  </array>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <!-- 毎時 :40。収集 (:00 前後) と事象ニュース生成 (:20) の後に回す。 -->
  <key>StartCalendarInterval</key>
  <dict><key>Minute</key><integer>40</integer></dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>${LOG}</string>
  <key>StandardErrorPath</key><string>${LOG}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <!-- ⚠ node は nodebrew 配下にあり、LaunchAgent の既定 PATH には入らない
         (実測: npx: command not found で配信が失敗した)。実体の場所を通す。 -->
    <string>${NODE_BIN}:${HOME}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: $LABEL (毎時 :40 / log: $LOG)"
launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | grep -E "state|runs" | head -3 || true
