#!/usr/bin/env bash
# アドバンスド (旧「写し」) を 3 時間ごとに書き出し、公開サイトと合わせて
# 配信する LaunchAgent (scripts/deploy_site.sh mirror を呼ぶ)。
#
#   bash scripts/install_mirror_launchagent.sh
#   bash scripts/install_mirror_launchagent.sh --uninstall
#
# 間隔を 3 時間にした理由 (2026-08-29):
#   Pages の配信回数には無料枠があり、毎時だと月 720 回で超える見込み。
#   3 時間なら月 240 回。アドバンスドは分析者向けの画面を広く見せる面であって
#   標準面ほど更新頻度が要らないので、失うのは最悪でも記事 25〜32 件ぶん
#   (到着 10.7 件/時)。足りなければ間隔を詰める — 判断材料は実際の不便さ。
#
# ⚠ 2026-10-05: 配信先は公開サイトと同じ Cloudflare Pages プロジェクトの
#    `/app/` 配下 (scripts/deploy_site.sh が公開面と合わせて 1 回で配信する)。
#    このスクリプト自身は「アドバンスドを作り直す」契機を 3 時間ごとに作るだけ。
set -euo pipefail

LABEL="com.cti.kuebiko-mirror"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/kuebiko-mirror.log"
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
    <string>mirror</string>
  </array>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <!-- 毎時 :50 に起こし、日本時間 2:50 から 3 時間ごとかどうかは配信スクリプト側
       (deploy_site.sh の KUEBIKO_SCHEDULED_MIRROR) が TZ=Asia/Tokyo で判断する。
       StartCalendarInterval は Mac の時間帯でしか動かないため、時の指定は持たない。
       ⚠ 時間帯の差が時間単位なら分 (:50) は変わらない。インド等 30 分ずれの時間帯では
       分もずれる (その場合は Minute を調整するか、このスクリプトを直す)。 -->
  <key>StartCalendarInterval</key>
  <dict><key>Minute</key><integer>50</integer></dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>${LOG}</string>
  <key>StandardErrorPath</key><string>${LOG}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>KUEBIKO_SCHEDULED_MIRROR</key><string>1</string>
    <key>TZ</key><string>Asia/Tokyo</string>
    <key>PATH</key>
    <!-- ⚠ node は nodebrew 配下にあり LaunchAgent の既定 PATH に入らない
         (公開サイトで npx: command not found を踏んだ)。実体の場所を通す。 -->
    <string>${NODE_BIN}:${HOME}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: $LABEL (毎時 :50 起動 → 日本時間 2:50 から 3 時間ごとに配信 / log: $LOG)"
