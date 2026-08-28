#!/usr/bin/env bash
# 運用画面の写しを **ローカルで確認する** (配信しない)。
#
#   scripts/preview_ops_mirror.sh [--skip-export] [--port 8788]
#
# ⚠ 試作中に毎回 Pages へ上げると配信回数の無料枠を食う (2026-08-29 利用者指摘。
#    実測: 公開サイトだけで 1 日 25 回配信していた)。**ローカルで通してから
#    1 回だけ配信する**。wrangler pages dev は Pages と同じ配信規則
#    (_redirects / _headers / SPA fallback) を再現するので、素の http.server より
#    本番に近い形で確かめられる。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/data/ops_mirror_dist"
PORT=8788
SKIP_EXPORT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-export) SKIP_EXPORT=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    *) echo "不明な引数: $1" >&2; exit 2 ;;
  esac
done

if [ "$SKIP_EXPORT" = "0" ]; then
  rm -rf "$DIST/data"
  uv run --directory "$ROOT" python "$ROOT/scripts/export_ops_mirror.py" \
    --base-url "${OPS_MIRROR_SOURCE:-http://127.0.0.1:8001}" --out "$DIST/data"
fi

echo
echo "ローカル確認: http://127.0.0.1:$PORT/data/meta.json"
echo "  (Ctrl-C で終了。**配信はされない**)"
echo
exec npx --yes wrangler@4 pages dev "$DIST" --port "$PORT" --ip 127.0.0.1
