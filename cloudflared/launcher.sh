#!/bin/bash
# Phase Diamond verify-mobile: cloudflared supervisor。
# flag file の存在で cloudflared subprocess を on/off。
# URL 検出時に Discord webhook に notify、URL を /app/data に出力。

set -u

# M1 (least-privilege): tunnel コンテナには専用サブディレクトリ ./data/mobile_tunnel だけを
# /app/data/mobile_tunnel に bind mount する (./data 全体は渡さない)。app コンテナとは
# host の ./data/mobile_tunnel/ を共有する (app は ./data 全体 mount 経由で同じパスを見る)。
DATA_DIR="/app/data/mobile_tunnel"
FLAG_FILE="${DATA_DIR}/.mobile_tunnel_enabled"
URL_FILE="${DATA_DIR}/.mobile_tunnel_url"
# named tunnel の token / hostname は data/ ファイル契約 (UI から管理、src/tools/mobile_tunnel_files.py
# が SSoT)。token は秘密なので 0600。app (uid 1001) が書き、この launcher (root) が読む。
TOKEN_FILE="${DATA_DIR}/.mobile_tunnel_token"
HOSTNAME_FILE="${DATA_DIR}/.mobile_tunnel_hostname"
TUNNEL_TARGET="${TUNNEL_TARGET:-http://readonly:8000}"
URL_REGEX='https://[a-zA-Z0-9-]+\.trycloudflare\.com'
POLL_INTERVAL=2

# 恒久安定 URL (named tunnel): token が設定されていれば quick tunnel ではなく named tunnel を
# 張る。公開ホスト名は Cloudflare 側で固定されるため **URL が再起動で一切変わらない** → churn も
# ops 通知スパムも消える。token は data/ ファイル (UI 管理) が正、無ければ env にフォールバック。
# 未設定なら従来の account-less quick tunnel に fall back する。
#
# 【セキュリティ】token はコンテナ env に置かず **--token 引数でファイルから渡す**。これにより
# cloudflared の env ダンプ (INF ログ) に token が載らない。env フォールバックは旧 .env/compose
# からの移行用で、初回に data/ ファイルへ seed したら以後は env を外してよい (compose から除去済)。
CF_TOKEN=""
CF_HOSTNAME=""

# token/hostname を data/ ファイルから解決する (無ければ env)。env のみ在ってファイルが無い
# 場合はファイルへ seed する (旧 env → ファイル契約への一度きりの移行)。
resolve_config() {
  # seed は temp→mv で atomic に (途中で kill されても partial file を残さない)。
  if [ ! -s "$TOKEN_FILE" ] && [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
    ( umask 077; printf '%s' "$CLOUDFLARE_TUNNEL_TOKEN" > "${TOKEN_FILE}.tmp" && mv "${TOKEN_FILE}.tmp" "$TOKEN_FILE" )
    echo "[supervisor] seeded token file from env (migration)"
  fi
  if [ ! -s "$HOSTNAME_FILE" ] && [ -n "${CLOUDFLARE_TUNNEL_HOSTNAME:-}" ]; then
    printf '%s' "$CLOUDFLARE_TUNNEL_HOSTNAME" > "${HOSTNAME_FILE}.tmp" && mv "${HOSTNAME_FILE}.tmp" "$HOSTNAME_FILE"
  fi
  if [ -s "$TOKEN_FILE" ]; then CF_TOKEN=$(cat "$TOKEN_FILE"); else CF_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-}"; fi
  if [ -s "$HOSTNAME_FILE" ]; then CF_HOSTNAME=$(cat "$HOSTNAME_FILE"); else CF_HOSTNAME="${CLOUDFLARE_TUNNEL_HOSTNAME:-}"; fi
  # 空白 (改行含む) を除去。hostname は scheme を剥がして素のホスト名にする。
  CF_TOKEN=$(printf '%s' "$CF_TOKEN" | tr -d '[:space:]')
  CF_HOSTNAME=$(printf '%s' "$CF_HOSTNAME" | tr -d '[:space:]' | sed -E 's#^https?://##; s#/+$##')
}

# token/hostname ファイルの mtime を連結した署名。UI 保存でファイルが変わると値が変わり、
# main loop が検知して cloudflared を再起動 → 再デプロイ不要で反映する。
config_sig() {
  t=""; h=""
  [ -e "$TOKEN_FILE" ] && t=$(stat -c '%Y' "$TOKEN_FILE" 2>/dev/null || true)
  [ -e "$HOSTNAME_FILE" ] && h=$(stat -c '%Y' "$HOSTNAME_FILE" 2>/dev/null || true)
  printf '%s:%s' "$t" "$h"
}
cfg_sig=""

# Default: 初回 deploy 時に flag file が無ければ env var に応じて作成
if [ ! -e "$FLAG_FILE" ] && [ ! -e "${DATA_DIR}/.mobile_tunnel_initialized" ]; then
  if [ "${MOBILE_TUNNEL_DEFAULT_ENABLED:-1}" = "1" ]; then
    touch "$FLAG_FILE"
    echo "[supervisor] default-on: created $FLAG_FILE"
  fi
  touch "${DATA_DIR}/.mobile_tunnel_initialized"
fi

cf_pid=""

cleanup() {
  echo "[supervisor] shutdown signaled"
  rm -f "$URL_FILE"
  if [ -n "$cf_pid" ] && kill -0 "$cf_pid" 2>/dev/null; then
    kill -TERM "$cf_pid" 2>/dev/null || true
    wait "$cf_pid" 2>/dev/null || true
  fi
  exit 0
}
trap cleanup INT TERM

# Discord ops へ 1 行 POST (実 HTTP status で成否判定。webhook 未設定なら skip)。
notify_ops() {
  msg="$1"
  if [ -z "${DISCORD_WEBHOOK_OPS:-}" ]; then
    echo "[supervisor] DISCORD_WEBHOOK_OPS not set, skipping notify"
    return
  fi
  # 【重要】--fail を使わず HTTP ステータスを明示確認する。curl は HTTP 404/401 でも
  # (request 自体は成功するので) exit 0 を返すため、旧実装は webhook 削除済み
  # (Unknown Webhook 10015) でも "ok" と誤記録していた。実 status code で判定する。
  payload=$(printf '{"content":"%s"}' "$msg")
  code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$DISCORD_WEBHOOK_OPS" \
       -H "Content-Type: application/json" -d "$payload" 2>/dev/null)
  if [ "$code" = "204" ] || [ "$code" = "200" ]; then
    echo "[supervisor] discord notify ok (HTTP $code)"
  else
    echo "[supervisor] discord notify FAILED (HTTP $code) — webhook 無効/削除の可能性。.env の DISCORD_WEBHOOK_OPS を確認すること"
  fi
}

start_tunnel() {
  rm -f "$URL_FILE"
  resolve_config           # data/ ファイル (無ければ env) から CF_TOKEN / CF_HOSTNAME を解決
  cfg_sig=$(config_sig)    # この起動時点の config 署名を記録 (UI 保存での変化検知の基準)
  if [ -n "$CF_TOKEN" ]; then
    # named tunnel: 公開ホスト名は Cloudflare 側で固定 → URL は再起動で不変。
    echo "[supervisor] starting cloudflared (named tunnel, stable URL)"
    if [ -n "$CF_HOSTNAME" ]; then
      echo "https://${CF_HOSTNAME}" > "$URL_FILE"
    fi
    # 【セキュリティ】token は env でなく --token 引数で渡す (cloudflared の env ダンプに載せない)。
    (
      cloudflared tunnel --no-autoupdate run --token "$CF_TOKEN" 2>&1 |
        while IFS= read -r line; do echo "$line"; done
    ) &
    cf_pid=$!
    # named tunnel は URL 不変のため通知は 1 コンテナ起動につき 1 回のみ (spam なし)。
    if [ -n "$CF_HOSTNAME" ]; then
      notify_ops "🌐 Kuebiko 閲覧専用 稼働中\\nhttps://${CF_HOSTNAME}/app/\\n\\n_(named tunnel — URL は固定・再起動で変わりません)_"
    fi
    return
  fi

  # account-less quick tunnel (fall back): URL は cloudflared 再起動で変わる。
  echo "[supervisor] starting cloudflared (quick tunnel, target=$TUNNEL_TARGET)"
  (
    cloudflared tunnel --no-autoupdate --url "$TUNNEL_TARGET" 2>&1 |
      while IFS= read -r line; do
        echo "$line"
        if [ ! -s "$URL_FILE" ]; then
          url=$(echo "$line" | grep -oE "$URL_REGEX" | head -1)
          if [ -n "$url" ]; then
            echo "$url" > "$URL_FILE"
            echo "[supervisor] tunnel URL: $url"
            notify_ops "🌐 Kuebiko 閲覧専用 URL ready\\n${url}/app/\\n\\n_(quick tunnel — URL は再起動で変わります。安定 URL には CLOUDFLARE_TUNNEL_TOKEN で named tunnel 化。現 URL は常に Web UI のスケジュール画面で確認可)_"
          fi
        fi
      done
  ) &
  cf_pid=$!
}

stop_tunnel() {
  echo "[supervisor] stopping cloudflared (pid=$cf_pid)"
  rm -f "$URL_FILE"
  if [ -n "$cf_pid" ] && kill -0 "$cf_pid" 2>/dev/null; then
    kill -TERM "$cf_pid" 2>/dev/null || true
    wait "$cf_pid" 2>/dev/null || true
  fi
  cf_pid=""
}

# 健全性チェック: quick tunnel は数日でセッションが切られ、cloudflared プロセスは
# 生きたまま control stream が失敗し続ける (再 URL 発行されず Discord 通知も出ない)
# 事故が起きる。プロセス生存だけでなく **公開 URL の到達性** を監視する。
#
# 【重要】再起動の判断は「自分のトンネルが不通」だけでなく「**インターネット
# (Cloudflare edge) が到達可能か**」も見る。インターネット断のときに再起動しても
# 新トンネルも繋がらず無駄 (むしろ churn を生む) なので再起動せず、既存 cloudflared
# の自力 retry に委ねて回復を待つ。再起動が有効なのは「ネットは生きているのに自分の
# セッションだけ死んでいる」ケースのみ。さらに backoff で連発を防ぐ。
HEALTH_INTERVAL=60          # 秒: 到達性チェック間隔
MAX_HEALTH_FAILS=3          # 連続失敗数 (= 約 3 分 到達不能で復旧判定)
MIN_RESTART_INTERVAL=300    # 秒: 再起動の最小間隔 (flap/無駄 churn 防止 backoff)
# インターネット (Cloudflare) 到達性チェック先。cloudflared は Cloudflare edge に
# 繋がる必要があるため 1.1.1.1 (Cloudflare 所有・常時稼働) を疎通先にする。
CONNECTIVITY_URL="https://1.1.1.1"
health_fail=0
last_health=0
last_restart=0

# Cloudflare edge (= インターネット) に到達できるか。0=到達, 1=断
internet_up() {
  curl -sS -o /dev/null --max-time 8 "$CONNECTIVITY_URL" 2>/dev/null
}

# Main supervisor loop
echo "[supervisor] starting (flag=$FLAG_FILE, poll=${POLL_INTERVAL}s, health=${HEALTH_INTERVAL}s, min_restart=${MIN_RESTART_INTERVAL}s)"
while true; do
  if [ -f "$FLAG_FILE" ]; then
    # 有効: cloudflared が走っていなければ起動
    if [ -z "$cf_pid" ] || ! kill -0 "$cf_pid" 2>/dev/null; then
      cf_pid=""
      health_fail=0
      start_tunnel
    elif [ "$(config_sig)" != "$cfg_sig" ]; then
      # UI で token/hostname が保存された (config 署名が変化) → 再起動して即反映 (再デプロイ不要)。
      echo "[supervisor] named tunnel config changed (UI 保存) → 再起動して反映"
      stop_tunnel
      start_tunnel
      health_fail=0
      last_health=$(date +%s)   # 再起動直後の warm-up を健全性誤検知しないよう次チェックを遅らせる
    else
      # プロセスは生きている → 公開 URL の到達性を定期チェック (dead-tunnel 検知)
      now=$(date +%s)
      if [ "$((now - last_health))" -ge "$HEALTH_INTERVAL" ]; then
        last_health=$now
        url=$(cat "$URL_FILE" 2>/dev/null || true)
        # URL_FILE が空 = まだ URL 未取得 (起動直後 or ネット断で取得待ち)。
        # この間は健全性判定の対象外 (cloudflared の自力 retry に任せる)。
        if [ -n "$url" ]; then
          # --fail 不使用: 2xx-5xx いずれの応答でもトンネル経路は生存と見なす
          # (app の一時的 5xx で再起動しない)。接続失敗/timeout のみ failure。
          if curl -sS -o /dev/null --max-time 12 "${url}/api/v1/runtime-flags" 2>/dev/null; then
            health_fail=0
          else
            health_fail=$((health_fail + 1))
            echo "[supervisor] tunnel unreachable (${health_fail}/${MAX_HEALTH_FAILS}): $url"
            if [ "$health_fail" -ge "$MAX_HEALTH_FAILS" ]; then
              health_fail=0
              if ! internet_up; then
                # インターネット断: 再起動しても無駄。cloudflared の自力 retry に委ね回復待ち。
                echo "[supervisor] インターネット断 (Cloudflare 不達) → 再起動 skip (回復待ち)"
              elif [ "$((now - last_restart))" -lt "$MIN_RESTART_INTERVAL" ]; then
                # backoff 中: 直近で再起動済み。連発を避ける。
                echo "[supervisor] 再起動 backoff 中 (前回から $((now - last_restart))s < ${MIN_RESTART_INTERVAL}s) → skip"
              else
                # ネットは生きているのに自セッションだけ死亡 = 再起動で復旧可能
                echo "[supervisor] tunnel dead (net OK) → cloudflared 再起動 (新 URL 発行 + Discord 通知)"
                stop_tunnel
                last_restart=$now
                last_health=$now   # 新トンネル warm-up 中の誤検知を避け次チェックを遅らせる
                start_tunnel
              fi
            fi
          fi
        fi
      fi
    fi
  else
    # 無効: cloudflared が走っていれば停止
    if [ -n "$cf_pid" ] && kill -0 "$cf_pid" 2>/dev/null; then
      stop_tunnel
    fi
  fi
  sleep "$POLL_INTERVAL"
done
