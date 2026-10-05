#!/usr/bin/env bash
# 旧サブドメイン (アドバンスドが単独で居たドメイン) を、新しい配信先
# (公開ドメインの /app 配下) への 301 リダイレクトだけにする。
#
#   scripts/deploy_mirror_redirect.sh
#
# ⚠ 2026-10-05: アドバンスド (旧「写し」) は公開サイトと同じドメインの /app 配下に
#    統合した (scripts/deploy_site.sh mirror)。このドメイン
#    ($CLOUDFLARE_MIRROR_PAGES_PROJECT) はもう中身を持たない — 古いブックマーク・
#    外部からのリンクを新しい場所へ送るためだけに残す。
#
# ⚠ **このスクリプトは手動で 1 回だけ流す想定**。LaunchAgent からは呼ばない
#    (中身が変わらないので毎時配信し直す意味がない)。
#
# 必要な資格情報・設定 (.env。§4 により DB には置かない):
#   CLOUDFLARE_API_TOKEN
#   CLOUDFLARE_ACCOUNT_ID
#   CLOUDFLARE_MIRROR_PAGES_PROJECT   旧サブドメインの Pages プロジェクト名
#   PUBLIC_SITE_ORIGIN                新しい配信先のオリジン (例 https://kuebiko.example)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/data/mirror_redirect_dist"

set -a && [ -f "$ROOT/.env" ] && . "$ROOT/.env"; set +a
: "${CLOUDFLARE_API_TOKEN:?.env に CLOUDFLARE_API_TOKEN がありません}"
: "${CLOUDFLARE_ACCOUNT_ID:?.env に CLOUDFLARE_ACCOUNT_ID がありません}"
: "${CLOUDFLARE_MIRROR_PAGES_PROJECT:?.env に CLOUDFLARE_MIRROR_PAGES_PROJECT がありません}"
: "${PUBLIC_SITE_ORIGIN:?.env に PUBLIC_SITE_ORIGIN (新しい配信先、例 https://kuebiko.example) がありません}"

# 旧サブドメインの画面の URL はもともと /app/… なので、そのまま同じパスへ (/app/app/… にしない)。
# それ以外 (トップ・古い /data 等) はアドバンスドのトップへ。
# スキームを落としてホストだけにする (_redirects のターゲットは常に https で書く)。
HOST="${PUBLIC_SITE_ORIGIN#http://}"
HOST="${HOST#https://}"
HOST="${HOST%/}"

rm -rf "$DIST"
mkdir -p "$DIST"
cat >| "$DIST/_redirects" <<REDIRECTS
/app/*  https://${HOST}/app/:splat   301
/*      https://${HOST}/app   301
REDIRECTS
cat >| "$DIST/index.html" <<HTML
<!doctype html>
<meta charset="utf-8">
<title>kuebiko</title>
<p>このページは移転しました。<a href="https://${HOST}/app">https://${HOST}/app</a> へお進みください。</p>
HTML

npx --yes wrangler@4 pages deploy "$DIST" \
  --project-name "$CLOUDFLARE_MIRROR_PAGES_PROJECT" --branch main --commit-dirty=true

echo "配信しました (旧サブドメイン → https://${HOST}/app への 301)"
