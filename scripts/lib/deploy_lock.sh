#!/usr/bin/env bash
# 公開面の配信のロックと成功の印 (P0-02)。scripts/deploy_site.sh から source して使う。
#
# ロックは mkdir (原子的) のディレクトリ。取った側が $LOCK/pid と $LOCK/started_epoch を書く。
# 古いロック = 持ち主の pid が死んでいる、または始まりから DEPLOY_LOCK_MAX_AGE 秒を超えた。
# pid の無い空のディレクトリ (旧版の残り・取った直後) は、ディレクトリの mtime で同じ上限を当てる。
# 古いロックは mv で脇へ退けてから取り直す (mv は原子的なので、同時に奪おうとした 2 本のうち
# 1 本だけが成功する)。時刻は epoch で持つ (時間帯に依存しない、R12)。
#
# macOS の bash 3.2 でも動くこと (LaunchAgent から呼ばれる)。

DEPLOY_LOCK_MAX_AGE="${DEPLOY_LOCK_MAX_AGE:-7200}"

_dl_now() { date +%s; }

# ファイル/ディレクトリの mtime (epoch)。BSD / GNU の両方に対応
_dl_mtime() {
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0
}

# 古いロックなら 0、生きていれば 1
_dl_is_stale() {
  local lock="$1" now pid started
  now="$(_dl_now)"
  if [ -f "$lock/pid" ]; then
    pid="$(cat "$lock/pid" 2>/dev/null || true)"
    case "$pid" in
      '' | *[!0-9]*) return 0 ;; # 壊れた pid は持ち主なしとみなす
    esac
    kill -0 "$pid" 2>/dev/null || return 0
    started="$(cat "$lock/started_epoch" 2>/dev/null || true)"
    case "$started" in
      '' | *[!0-9]*) started="$(_dl_mtime "$lock")" ;;
    esac
    [ $((now - started)) -gt "$DEPLOY_LOCK_MAX_AGE" ] && return 0
    return 1
  fi
  # pid の無いロック: mtime で判定 (取った直後の短い隙間は若いので奪わない)
  [ $((now - $(_dl_mtime "$lock"))) -gt "$DEPLOY_LOCK_MAX_AGE" ] && return 0
  return 1
}

# 古いロックを脇へ退ける。成功 (退けた) で 0。他の本に先を越された場合は 1
_dl_take_stale() {
  local lock="$1" aside="$1.stale.$$"
  mv "$lock" "$aside" 2>/dev/null || return 1
  rm -rf "$aside"
  return 0
}

# acquire_lock LOCK [待つ秒数=2400] [間隔=10]
# 取れたら 0 (pid と開始時刻を書く)、時間切れで 1。
acquire_lock() {
  local lock="$1" wait_secs="${2:-2400}" interval="${3:-10}" waited=0
  while :; do
    if mkdir "$lock" 2>/dev/null; then
      echo "$$" >"$lock/pid"
      _dl_now >"$lock/started_epoch"
      return 0
    fi
    if [ -d "$lock" ] && _dl_is_stale "$lock"; then
      _dl_take_stale "$lock" && continue
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
