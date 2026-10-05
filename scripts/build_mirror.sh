#!/usr/bin/env bash
# アドバンスド (旧「写し」) を組み立てる (書き出し + アドバンスド用ビルド)。**配信はしない**。
#
#   scripts/build_mirror.sh [--skip-export]
#
# ⚠ 確認用と配信用で組み立てを別々に持つと必ずずれる。実際、確認用は
#    frontend を作り直しておらず、直したはずのコードを確認できていなかった
#    (2026-08-29)。組み立ては **この 1 本だけ** が知っている。
#
# ⚠ 2026-10-05: アドバンスドは公開サイトと同じドメインの `/app/` 配下で配信する
#    ように変更した (旧: 別ドメインに匿名公開)。vite の base が運用画面と同じ
#    `/app/` になるため、index.html の PWA 参照 (/app/pwa/…) はそのまま使える。
#    旧版にあった `/app/pwa/ → /pwa/` の sed 書き換えは、base が揺れなくなった
#    ので削除した (残すと参照が壊れる)。このディレクトリ (data/mirror_dist) は
#    scripts/deploy_site.sh が公開サイトと合わせて `app/` 配下へ丸ごとコピーする。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/data/mirror_dist"
BASE_URL="${MIRROR_SOURCE:-http://127.0.0.1:8001}"

if [ "${1:-}" != "--skip-export" ]; then
  # 稼働中の API を叩いて写す。落ちていれば失敗する = 空のアドバンスドを配らない。
  rm -rf "$DIST/data"
  uv run --directory "$ROOT" python "$ROOT/scripts/export_mirror.py" \
    --base-url "$BASE_URL" --out "$DIST/data"
fi

# ⚠ **アドバンスド用ビルド** (VITE_MIRROR=1)。通常ビルドを置くと API を叩きに行って
#    全部失敗し、しかも帯 (いつ時点の情報か・標準/アドバンスド切替) が出ない。
#    静的ファイルは /adv/ に置く (画面の URL は /app/…。frontend/vite.config.ts の base)。
#    データの参照先も組み立て後の置き場所 (/adv/data) に向ける。
( cd "$ROOT/frontend" && VITE_MIRROR=1 VITE_MIRROR_DATA=/adv/data npm run build >/dev/null )
rm -rf "$DIST/assets" "$DIST/index.html" "$DIST/pwa"
cp -R "$ROOT/frontend/dist/." "$DIST/"
# index.html と manifest の PWA 参照は /app/pwa/… で直書き (vite の base が効かない)。置き場所の /adv/pwa/ へ
sed -i '' 's#"/app/pwa/#"/adv/pwa/#g' "$DIST/index.html" "$DIST/pwa/manifest.webmanifest"
# 通常ビルドへ戻す (ローカルの運用画面がアドバンスドビルドのままにならないように)
( cd "$ROOT/frontend" && npm run build >/dev/null )
