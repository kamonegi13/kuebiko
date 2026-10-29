#!/usr/bin/env bash
# 公開面の配信のロックと成功の印 (P0-02)。scripts/deploy_site.sh から source して使う。
#
# ロックは mkdir (原子的) のディレクトリ。取った側が $LOCK/pid と $LOCK/started_epoch を書く。
# 古いロック = 持ち主の pid が存在しない、または pid が配信の本体でない (再利用)。生きた持ち主のロックは
# 時間が経っても奪わない。pid の無い空のディレクトリ (旧版の残り) は mtime が DEPLOY_LOCK_MAX_AGE 秒より古く、
# かつ他に配信の本体が動いていないときだけ奪う。
# 古いロックは mv で脇へ退けてから取り直す (mv は原子的なので、同時に奪おうとした 2 本のうち
# 1 本だけが成功する)。時刻は epoch で持つ (時間帯に依存しない、R12)。
#
# macOS の bash 3.2 でも動くこと (LaunchAgent から呼ばれる)。

DEPLOY_LOCK_MAX_AGE="${DEPLOY_LOCK_MAX_AGE:-7200}"
# 持ち主のプロセスの command にこの文字列が入っていれば「配信の本体」とみなす (pid の再利用の見分け)
DEPLOY_LOCK_OWNER_PATTERN="${DEPLOY_LOCK_OWNER_PATTERN:-deploy_site.sh}"

_dl_now() { date +%s; }

# ファイル/ディレクトリの mtime (epoch)。BSD / GNU の両方に対応
_dl_mtime() {
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0
}

# 配信の本体として動いている pid が自分以外にあるか (あれば 0)
_dl_other_owner_running() {
  local p
  for p in $(pgrep -f "$DEPLOY_LOCK_OWNER_PATTERN" 2>/dev/null); do
    [ "$p" = "$$" ] || return 0
  done
  return 1
}

# 古いロックなら 0、生きていれば 1。
# 生きている持ち主のロックは時間が経っていても奪わない (長く掛かる配信を壊さない)。
# 奪ってよいのは pid が存在しない / その pid が配信の本体でない (pid の再利用) ときだけ。
# pid の無い空ディレクトリ (旧版の残り) は、mtime が古く、かつ他に配信の本体が動いていないときだけ。
_dl_is_stale() {
  local lock="$1" pid cmd
  if [ -f "$lock/pid" ]; then
    pid="$(cat "$lock/pid" 2>/dev/null || true)"
    case "$pid" in
      '' | *[!0-9]*) return 0 ;; # 壊れた pid は持ち主なしとみなす
    esac
    kill -0 "$pid" 2>/dev/null || return 0
    cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    case "$cmd" in
      *"$DEPLOY_LOCK_OWNER_PATTERN"*) return 1 ;;
    esac
    return 0
  fi
  [ $(($(_dl_now) - $(_dl_mtime "$lock"))) -gt "$DEPLOY_LOCK_MAX_AGE" ] || return 1
  _dl_other_owner_running && return 1
  return 0
}

# 古いロックを脇へ退ける。judged_pid = 判定したときに読んだ pid (無ければ空)。
# 退けた先の pid が判定時と違えば、判定と mv の間に他の本が取り直した生きたロックなので戻す。
_dl_take_stale() {
  local lock="$1" judged_pid="$2" aside="$1.stale.$$" moved_pid
  mv "$lock" "$aside" 2>/dev/null || return 1
  moved_pid="$(cat "$aside/pid" 2>/dev/null || true)"
  if [ "$moved_pid" != "$judged_pid" ]; then
    [ -e "$lock" ] || mv "$aside" "$lock" 2>/dev/null || true
    return 1
  fi
  rm -rf "$aside"
  return 0
}

# acquire_lock LOCK [待つ秒数=2400] [間隔=10]
# 取れたら 0 (pid と開始時刻を書く)、時間切れで 1。
acquire_lock() {
  local lock="$1" wait_secs="${2:-2400}" interval="${3:-10}" waited=0 judged_pid
  while :; do
    if mkdir "$lock" 2>/dev/null; then
      echo "$$" >"$lock/pid"
      _dl_now >"$lock/started_epoch"
      return 0
    fi
    if [ -d "$lock" ]; then
      judged_pid="$(cat "$lock/pid" 2>/dev/null || true)"
      if _dl_is_stale "$lock" && _dl_take_stale "$lock" "$judged_pid"; then
        continue
      fi
    fi
    [ "$waited" -ge "$wait_secs" ] && return 1
    sleep "$interval"
    waited=$((waited + interval))
  done
}

# release_lock LOCK — 自分が持っているときだけ消す (奪われた後に他人のロックを消さない)
release_lock() {
  local lock="$1"
  [ -f "$lock/pid" ] && [ "$(cat "$lock/pid" 2>/dev/null)" = "$$" ] || return 0
  rm -f "$lock/pid" "$lock/started_epoch"
  rmdir "$lock" 2>/dev/null || true
}

# write_success_stamp FILE SIDE DIGEST RESULT WRANGLER
# 配信の成功と「変更なし」の両方で呼ぶ。一時ファイル + mv で原子的に書く。
# RESULT: deployed | unchanged
write_success_stamp() {
  local file="$1" side="$2" digest="$3" result="$4" wrangler="$5" tmp
  tmp="$file.tmp.$$"
  printf '{"ts_epoch": %s, "side": "%s", "digest": "%s", "result": "%s", "wrangler": "%s"}\n' \
    "$(_dl_now)" "$side" "$digest" "$result" "$wrangler" >"$tmp" || return 1
  mv "$tmp" "$file"
}

# stamp_age_seconds FILE — 成功の印の経過秒。無い/壊れていれば空を出して 1
stamp_age_seconds() {
  local ts
  ts="$(sed -n 's/.*"ts_epoch": *\([0-9][0-9]*\).*/\1/p' "$1" 2>/dev/null | head -1)"
  [ -n "$ts" ] || return 1
  echo $(($(_dl_now) - ts))
}

# kill_tree PID — 子孫から順に止める (npx -> node のように孫が残らないように)
_dl_kill_tree() {
  local c
  for c in $(pgrep -P "$1" 2>/dev/null); do _dl_kill_tree "$c"; done
  kill "$1" 2>/dev/null || true
}

# run_with_timeout SECS COMPLETE_MARK COMMAND... (bash 3.2 で動く。timeout コマンドは macOS に無い)
# 出力は一時ファイルに溜め、終了後に表示する。COMPLETE_MARK の行が出た後に終わらない場合は
# DEPLOY_COMPLETE_GRACE 秒 (既定 30) 待って止め、成功として扱う。
# 戻り値: 0 = 成功 (正常終了、または完了の行が出ていた)、124 = 時間切れ (完了の行なし)、他 = コマンドの失敗
run_with_timeout() {
  local secs="$1" mark="$2" log pid elapsed=0 seen_at="" rc=0 killed=0
  shift 2
  log="$(mktemp "${TMPDIR:-/tmp}/deploy_run.XXXXXX")" || return 1
  "$@" >"$log" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [ -z "$seen_at" ] && grep -q "$mark" "$log" 2>/dev/null; then seen_at="$elapsed"; fi
    if [ -n "$seen_at" ] && [ $((elapsed - seen_at)) -ge "${DEPLOY_COMPLETE_GRACE:-30}" ]; then killed=1; break; fi
    if [ "$elapsed" -ge "$secs" ]; then killed=1; break; fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  if [ "$killed" = "1" ]; then _dl_kill_tree "$pid"; fi
  wait "$pid" 2>/dev/null || rc=$?
  cat "$log"
  if grep -q "$mark" "$log" 2>/dev/null; then rc=0
  elif [ "$killed" = "1" ]; then rc=124
  fi
  rm -f "$log"
  return "$rc"
}
