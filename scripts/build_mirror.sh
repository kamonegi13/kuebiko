#!/usr/bin/env bash
# 写しを組み立てる (書き出し + 写し用ビルド)。**配信はしない**。
#
#   scripts/build_mirror.sh [--skip-export]
#
# ⚠ 確認用と配信用で組み立てを別々に持つと必ずずれる。実際、確認用は
#    frontend を作り直しておらず、直したはずのコードを確認できていなかった
#    (2026-08-29)。組み立ては **この 1 本だけ** が知っている。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/data/mirror_dist"
BASE_URL="${MIRROR_SOURCE:-http://127.0.0.1:8001}"

# 標準⇄アドバンスドの切替導線が使う公開サイトのオリジン。.env から読む (未設定なら
# 導線を無効表示にするだけなので、テスト等で未設定でもビルドは通る)。
set -a && [ -f "$ROOT/.env" ] && . "$ROOT/.env"; set +a
PUBLIC_SITE_ORIGIN="${PUBLIC_SITE_ORIGIN:-}"

if [ "${1:-}" != "--skip-export" ]; then
  # 稼働中の API を叩いて写す。落ちていれば失敗する = 空の写しを配らない。
  rm -rf "$DIST/data"
  uv run --directory "$ROOT" python "$ROOT/scripts/export_mirror.py" \
    --base-url "$BASE_URL" --out "$DIST/data"
fi

# ⚠ **写し用ビルド** (VITE_MIRROR=1)。通常ビルドを置くと API を叩きに行って
#    全部失敗し、しかも「写しである」帯が出ない。
( cd "$ROOT/frontend" && VITE_MIRROR=1 VITE_MIRROR_DATA=/data \
    VITE_PUBLIC_ORIGIN="$PUBLIC_SITE_ORIGIN" npm run build >/dev/null )
rm -rf "$DIST/assets" "$DIST/index.html" "$DIST/pwa"
cp -R "$ROOT/frontend/dist/." "$DIST/"
# ⚠ PWA の参照は index.html に /app/pwa/… で直書きされ vite の base が効かない。
#    写しはルート直下なのでその分を書き換える (残すと manifest が Access の
#    ログインへ飛ばされ CORS で落ちる。2026-08-29 実測)。
sed -i '' 's#"/app/pwa/#"/pwa/#g' "$DIST/index.html"
# manifest のアイコンも /app/pwa/… で直書きされている (写しでは /pwa/ に置く。残すとアイコンが 404)
sed -i '' 's#"/app/pwa/#"/pwa/#g' "$DIST/pwa/manifest.webmanifest"
# 通常ビルドへ戻す (ローカルの運用画面が写しビルドのままにならないように)
( cd "$ROOT/frontend" && npm run build >/dev/null )
