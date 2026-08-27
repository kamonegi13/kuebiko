#!/usr/bin/env bash
# 公開サイトを Cloudflare Pages へ配信する。
#
#   scripts/deploy_public_site.sh [--force]
#
# **中身が変わっていなければ何もしない**。生成が無い時間帯は 1 日の 1/3 以上あり
# (実測 2026-08-26)、毎時そのまま上げると中身の変わらないデプロイになる。
# Pages の無料枠に回数の上限がある前提で、無駄打ちを避ける。
#
# 必要な資格情報 (.env に置く。§4 により DB には置かない):
#   CLOUDFLARE_API_TOKEN     権限は Account → Cloudflare Pages → Edit のみで足りる
#   CLOUDFLARE_ACCOUNT_ID
#   CLOUDFLARE_PAGES_PROJECT 例: kuebiko-news
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/data/public_site_dist"
STAMP="$ROOT/data/public_site_deployed.sha256"

set -a && [ -f "$ROOT/.env" ] && . "$ROOT/.env"; set +a
: "${CLOUDFLARE_API_TOKEN:?.env に CLOUDFLARE_API_TOKEN がありません}"
: "${CLOUDFLARE_ACCOUNT_ID:?.env に CLOUDFLARE_ACCOUNT_ID がありません}"
: "${CLOUDFLARE_PAGES_PROJECT:?.env に CLOUDFLARE_PAGES_PROJECT がありません}"

"$ROOT/scripts/build_public_site.sh" "$DIST" >/dev/null

# ⚠ 変更検知は **データ + 配信物の両方** で行う。データの sha だけ見ていると、
# フロント (ログイン導線・manifest 等) だけの変更が永久に配信されない
# (2026-08-27 実測: PWA ログイン修正がデータ不変のため配信されずに残った)。
DIGEST="$(cd "$DIST" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256 | cut -d' ' -f1)"
PREVIOUS="$(cat "$STAMP" 2>/dev/null || true)"

if [ "${1:-}" != "--force" ] && [ "$DIGEST" = "$PREVIOUS" ]; then
  echo "変更なし (sha256 ${DIGEST:0:12}) — 配信しない"
  exit 0
fi

npx --yes wrangler@latest pages deploy "$DIST" \
  --project-name "$CLOUDFLARE_PAGES_PROJECT" \
  --branch main \
  --commit-dirty=true

printf '%s' "$DIGEST" >| "$STAMP"
echo "配信しました (sha256 ${DIGEST:0:12})"
