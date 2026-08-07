#!/usr/bin/env bash
# OrbStack VM が macOS の sleep/wake で固まったまま復帰しない事象からの自動復旧
# (2026-08-02)。LaunchAgent から 5 分間隔で起動される。
#
# 背景: 本機はサーバではなくノート PC で、持ち出し中はスリープする。スリープ自体は
# 前提条件であり、収集は wake 後の misfire 追い付きで自己回復する (実測: 記事の
# 取りこぼしゼロ)。**問題は VM が wake に失敗して固まるケース** (2026-07-10 実績) で、
# これだけは人が気づくまで完全停止する。それを「数十分の遅延」に格下げするのが目的。
#
# 制御と可視化 (src/tools/host_watchdog_files.py が契約の SSoT):
#   - 有効/無効は data/host_watchdog/.orbstack_watchdog_enabled の有無 (UI から切替)
#   - 状態は state.json、履歴は watchdog.log に書く (どちらも data/ 配下 = UI から読める)
#   - 導入/削除のみターミナル (UI はコンテナ内なので launchctl を操作できない)
#
# 設計上の安全弁 (誤爆すると in-flight のパイプラインを殺すため慎重に):
#   1. macOS + orbctl がある環境でのみ動く (Linux サーバでは即 no-op で終了)
#   2. OrbStack が意図的に停止されている場合は何もしない (Running のときだけ介入)
#   3. wake 直後は判定しない (VM の復帰待ち。DarkWake の数十秒で誤爆させない)
#   4. 連続 N 回失敗して初めて復旧する (一過性のブリップで再起動しない)
#   5. 復旧試行は 1 時間あたり上限つき (再起動ループの防止)
#   6. docker が無応答 = パイプラインも動いていない → 復旧で失う in-flight 処理はない
set -uo pipefail

REPO="${KUEBIKO_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
DATA_DIR="${KUEBIKO_WATCHDOG_DATA_DIR:-${REPO}/data/host_watchdog}"
ENABLED_FLAG="${DATA_DIR}/.orbstack_watchdog_enabled"
STATE_FILE="${DATA_DIR}/state.json"
LOG_FILE="${DATA_DIR}/watchdog.log"
ENV_FILE="${KUEBIKO_ENV_FILE:-${REPO}/.env}"

# --- 閾値 (すべて安全側に倒す) ---
# wake 直後の猶予。DarkWake は 45 秒前後で終わるため、それより十分長くして
# 「スリープ中に見てしまう」誤爆を構造的に防ぐ。
MIN_AWAKE_SECONDS="${WATCHDOG_MIN_AWAKE_SECONDS:-180}"
# 連続失敗がこの回数に達したら復旧 (5 分間隔 × 3 = 約 15 分の持続的無応答)
FAIL_THRESHOLD="${WATCHDOG_FAIL_THRESHOLD:-3}"
# 外部コマンドのハード timeout (ハング時に watchdog 自身が固まらないため)
PROBE_TIMEOUT="${WATCHDOG_PROBE_TIMEOUT:-15}"
# 1 時間あたりの復旧試行上限
MAX_RECOVERIES_PER_HOUR="${WATCHDOG_MAX_RECOVERIES:-3}"
# ログの保持行数 (肥大化防止)
MAX_LOG_LINES=500

mkdir -p "$DATA_DIR"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"
  # 古い行を落とす (append-only で肥大化させない)
  if [[ -f "$LOG_FILE" ]] && (( $(wc -l <"$LOG_FILE") > MAX_LOG_LINES )); then
    tail -n "$MAX_LOG_LINES" "$LOG_FILE" >"${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
  fi
}

# 状態は JSON (UI が読む)。checked_at は「watchdog が生きている」ことの証拠になる。
write_state() {
  local fails="$1" recoveries="$2" window_start="$3" status="$4" detail="${5:-}"
  /usr/bin/python3 - "$STATE_FILE" "$fails" "$recoveries" "$window_start" "$status" "$detail" <<'PY' 2>/dev/null || true
import json, os, sys, time
path, fails, recoveries, window_start, status, detail = sys.argv[1:7]
prev = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        prev = {}
now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
state = {
    "checked_at": now,
    "status": status,
    "consecutive_failures": int(fails),
    "recoveries_this_hour": int(recoveries),
    "window_start": int(window_start),
    "detail": detail,
    "last_recovery_at": prev.get("last_recovery_at", ""),
    "last_recovery_result": prev.get("last_recovery_result", ""),
}
if status in ("recovered", "recovery_failed"):
    state["last_recovery_at"] = now
    state["last_recovery_result"] = detail or status
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False)
os.replace(tmp, path)
PY
}

read_counters() {
  /usr/bin/python3 - "$STATE_FILE" <<'PY' 2>/dev/null || echo "0 0 0"
import json, sys, os
path = sys.argv[1]
d = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
print(d.get("consecutive_failures", 0), d.get("recoveries_this_hour", 0), d.get("window_start", 0))
PY
}

# macOS には timeout(1) が無いのでバックグラウンド + 監視で代替する。
# **すべての外部コマンドをこれで包む** — ハングを扱う watchdog 自身がハングしないため。
run_with_timeout() {
  local secs="$1"; shift
  "$@" >/dev/null 2>&1 &
  local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if (( waited >= secs )); then
      kill -9 "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      return 124
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid"
}

# 出力が要る場合の timeout 付き実行 (stdout を返す。timeout / 失敗時は空)
capture_with_timeout() {
  local secs="$1" out; shift
  out="$("$@" 2>/dev/null & pid=$!;
        waited=0
        while kill -0 $pid 2>/dev/null; do
          if [ $waited -ge "$secs" ]; then kill -9 $pid 2>/dev/null; break; fi
          sleep 1; waited=$((waited+1))
        done
        wait $pid 2>/dev/null)" || true
  printf '%s' "$out"
}

seconds_since_wake() {
  /usr/bin/python3 - <<'PY' 2>/dev/null || echo 99999
import re, subprocess, time
out = subprocess.run(["sysctl", "-n", "kern.waketime"], capture_output=True, text=True).stdout
m = re.search(r"sec = (\d+)", out)
print(int(time.time()) - int(m.group(1)) if m else 99999)
PY
}

notify_ops() {
  # Discord ops への通知は best-effort。webhook URL はログに出さない (CLAUDE.md §4)。
  local msg="$1" url
  [[ -f "$ENV_FILE" ]] || return 0
  url="$(grep -E '^DISCORD_WEBHOOK_OPS=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
  [[ -n "$url" ]] || return 0
  curl -s -m 10 -H 'Content-Type: application/json' \
    -d "$(/usr/bin/python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' "$msg")" \
    "$url" >/dev/null 2>&1 || true
}

# ---- 0. 対象プラットフォームか (Linux サーバへ移した場合はここで終了) ----
if [[ "$(uname -s)" != "Darwin" ]] || ! command -v orbctl >/dev/null 2>&1; then
  # ログも状態も書かない: この環境には無関係なので痕跡を残さない
  exit 0
fi

# ---- 0b. 有効化されているか (UI のトグル = フラグファイル) ----
if [[ ! -f "$ENABLED_FLAG" ]]; then
  exit 0
fi

# ---- 1. OrbStack が意図的に停止されているなら介入しない ----
orb_status="$(capture_with_timeout 10 orbctl status | head -1 | tr -d '[:space:]')"
if [[ -z "$orb_status" ]]; then
  # orbctl 自体が無応答 = VM が固まっている可能性。probe に進む
  orb_status="Unresponsive"
elif [[ "$orb_status" != "Running" ]]; then
  write_state 0 0 0 "idle" "orbstack=${orb_status} (意図的な停止とみなし介入しない)"
  exit 0
fi

# ---- 2. wake 直後は判定しない (VM の復帰待ち / DarkWake 誤爆防止) ----
awake="$(seconds_since_wake)"
if (( awake < MIN_AWAKE_SECONDS )); then
  exit 0
fi

# ---- 3. docker の応答性を probe ----
read -r fails recoveries window_start <<<"$(read_counters)"
now="$(date +%s)"
if (( now - window_start > 3600 )); then
  recoveries=0
  window_start="$now"
fi

if run_with_timeout "$PROBE_TIMEOUT" docker ps; then
  if (( fails > 0 )); then
    log "docker 応答を回復 (連続失敗 ${fails} 回でリセット)"
  fi
  write_state 0 "$recoveries" "$window_start" "healthy" ""
  exit 0
fi

fails=$((fails + 1))
log "docker 無応答 (連続 ${fails} 回 / 閾値 ${FAIL_THRESHOLD}、orbctl=${orb_status}、wake 後 ${awake}s)"

if (( fails < FAIL_THRESHOLD )); then
  write_state "$fails" "$recoveries" "$window_start" "degraded" \
    "docker 無応答 ${fails}/${FAIL_THRESHOLD} 回 (閾値まで様子見)"
  exit 0
fi

# ---- 4. 復旧 (上限つき) ----
if (( recoveries >= MAX_RECOVERIES_PER_HOUR )); then
  log "復旧上限 (${MAX_RECOVERIES_PER_HOUR}/時) に到達。手動確認が必要"
  notify_ops "⚠️ kuebiko: OrbStack が復旧できません (自動復旧を ${MAX_RECOVERIES_PER_HOUR} 回試行)。手動確認をお願いします"
  write_state "$fails" "$recoveries" "$window_start" "recovery_failed" "復旧上限に到達 (手動確認が必要)"
  exit 1
fi

recoveries=$((recoveries + 1))
log "復旧を開始 (試行 ${recoveries}/${MAX_RECOVERIES_PER_HOUR})"

# 4a. まず穏当に restart。docker が無応答 = 走っている処理は無いので安全。
run_with_timeout 90 orbctl restart
sleep 10
if run_with_timeout "$PROBE_TIMEOUT" docker ps; then
  log "orbctl restart で復旧"
  notify_ops "🔧 kuebiko: OrbStack の無応答を検知し自動復旧しました (orbctl restart)"
  write_state 0 "$recoveries" "$window_start" "recovered" "orbctl restart で復旧"
  exit 0
fi

# 4b. 効かない場合のみ vmgr helper を落とす (2026-07-10 に唯一効いた手順)。
log "orbctl restart で復旧せず。vmgr helper を再生成する"
pkill -f "OrbStack Helper vmgr" 2>/dev/null || true
sleep 5
run_with_timeout 90 orbctl start
sleep 15
if run_with_timeout "$PROBE_TIMEOUT" docker ps; then
  log "vmgr 再生成で復旧"
  notify_ops "🔧 kuebiko: OrbStack の無応答を検知し自動復旧しました (vmgr 再生成)"
  write_state 0 "$recoveries" "$window_start" "recovered" "vmgr 再生成で復旧"
  exit 0
fi

log "自動復旧に失敗 (試行 ${recoveries})"
notify_ops "⚠️ kuebiko: OrbStack の自動復旧に失敗しました (試行 ${recoveries}/${MAX_RECOVERIES_PER_HOUR})"
write_state "$fails" "$recoveries" "$window_start" "recovery_failed" "自動復旧に失敗 (試行 ${recoveries})"
exit 1
