#!/usr/bin/env bash
# 運用画面 (Tier1) の写しを Cloudflare Pages へ配信する。
#
#   scripts/deploy_mirror.sh [--force]
#
# ⚠ **先に scripts/preview_mirror.sh でローカル確認する**。試作中に毎回
#    上げると配信回数の無料枠を食う (2026-08-29 利用者指摘。実測: 公開サイト
#    だけで 1 日 25 回配信していた)。通ってから 1 回だけ配信する。
#
# ⚠ **公開サイトとは別の Pages プロジェクトへ出す**。同じプロジェクトに混ぜると、
#    パス単位の保護が漏れたときに運用の中身が匿名で読める。プロジェクトごと
#    Cloudflare Access を掛ける前提。
#
# 必要な資格情報 (.env。§4 により DB には置かない):
#   CLOUDFLARE_API_TOKEN
#   CLOUDFLARE_ACCOUNT_ID
#   CLOUDFLARE_MIRROR_PAGES_PROJECT   例: kuebiko-ops  (公開サイトとは別の名前)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/data/mirror_dist"
STAMP="$ROOT/data/mirror_deployed.sha256"
BASE_URL="${MIRROR_SOURCE:-http://127.0.0.1:8001}"

set -a && [ -f "$ROOT/.env" ] && . "$ROOT/.env"; set +a
: "${CLOUDFLARE_API_TOKEN:?.env に CLOUDFLARE_API_TOKEN がありません}"
: "${CLOUDFLARE_ACCOUNT_ID:?.env に CLOUDFLARE_ACCOUNT_ID がありません}"
: "${CLOUDFLARE_MIRROR_PAGES_PROJECT:?.env に CLOUDFLARE_MIRROR_PAGES_PROJECT がありません}"

# 稼働中の API を叩いて写す。落ちていれば失敗する = 空の写しを配らない。
rm -rf "$DIST/data"
uv run --directory "$ROOT" python "$ROOT/scripts/export_mirror.py" \
  --base-url "$BASE_URL" --out "$DIST/data"

# ⚠ **写し用ビルド**で作る (VITE_MIRROR=1)。通常ビルドを置くと、
#    API を叩きに行って全部失敗し、しかも「写しである」帯が出ない。
( cd "$ROOT/frontend" && VITE_MIRROR=1 VITE_MIRROR_DATA=/data npm run build >/dev/null )
rm -rf "$DIST/assets" "$DIST/index.html" "$DIST/pwa"
cp -R "$ROOT/frontend/dist/." "$DIST/"
# 通常ビルドへ戻す (ローカルの運用画面が写しビルドのままにならないように)
( cd "$ROOT/frontend" && npm run build >/dev/null )
DIGEST="$(cd "$DIST" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256 | cut -d' ' -f1)"
if [ "${1:-}" != "--force" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$DIGEST" ]; then
  echo "変更なし (sha256 ${DIGEST:0:12}) — 配信しない"
  exit 0
fi

npx --yes wrangler@4 pages deploy "$DIST" \
  --project-name "$CLOUDFLARE_MIRROR_PAGES_PROJECT" --branch main --commit-dirty=true

echo "$DIGEST" > "$STAMP"
echo "配信しました (sha256 ${DIGEST:0:12})"
