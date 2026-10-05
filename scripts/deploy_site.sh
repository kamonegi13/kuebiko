#!/usr/bin/env bash
# 公開サイト (標準面、/news) とアドバンスド (旧「写し」、/app) を同じ Cloudflare
# Pages プロジェクトへ **1 回の配信でまとめて**出す (2026-10-05)。
#
#   scripts/deploy_site.sh public [--force]   # 標準面を作り直してから配信
#   scripts/deploy_site.sh mirror [--force]   # アドバンスド面を作り直してから配信
#
# ⚠ **2 つの LaunchAgent (public = 毎時 / mirror = 3 時間ごと) は両方この 1 本を
#    呼ぶ** (scripts/install_public_site_launchagent.sh /
#    scripts/install_mirror_launchagent.sh)。どちらが呼んでも、配信物は
#    ディスク上の両方の dist (data/public_site_dist + data/mirror_dist) を
#    組み合わせて作る — 片方だけ作り直して配信しても、もう片方は直前の dist が
#    そのまま載るので、呼ばれなかった面が消えたり古くなったりはしない。
#
# ⚠ **どちらかの dist が無ければ配信せず失敗する**。combined デプロイは
#    1 ディレクトリなので、中身が空の面を配ると公開済みの面まで同時に消える。
#
# 各面の書き出し関門 (public: export_public_site.py の _FORBIDDEN_KEYS /
# mirror: export_mirror.py の _final_gate) は build_*.sh の中で変わらず動く。
#
# 必要な資格情報 (.env。§4 により DB には置かない):
#   CLOUDFLARE_API_TOKEN     権限は Account → Cloudflare Pages → Edit のみで足りる
#   CLOUDFLARE_ACCOUNT_ID
#   CLOUDFLARE_PAGES_PROJECT 例: kuebiko-news (公開ドメインに紐づくプロジェクト)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLIC_DIST="$ROOT/data/public_site_dist"
MIRROR_DIST="$ROOT/data/mirror_dist"
COMBINED="$ROOT/data/combined_site_dist"
STAMP="$ROOT/data/combined_site_deployed.sha256"
# Cloudflare Pages の 1 デプロイあたりファイル数上限 (20,000)。余裕を持って切る。
MAX_FILES=20000

SIDE="${1:-}"
case "$SIDE" in
  public)
    shift
    bash "$ROOT/scripts/build_public_site.sh" >/dev/null
    ;;
  mirror)
    shift
    bash "$ROOT/scripts/build_mirror.sh" >/dev/null
    ;;
  *)
    echo "使い方: $0 public|mirror [--force]" >&2
    exit 1
    ;;
esac

[ -d "$PUBLIC_DIST" ] || {
  echo "公開サイトの配信物がありません ($PUBLIC_DIST)。先に scripts/build_public_site.sh を実行してください" >&2
  exit 1
}
[ -d "$MIRROR_DIST" ] || {
  echo "アドバンスドの配信物がありません ($MIRROR_DIST)。先に scripts/build_mirror.sh を実行してください" >&2
  exit 1
}

set -a && [ -f "$ROOT/.env" ] && . "$ROOT/.env"; set +a
: "${CLOUDFLARE_API_TOKEN:?.env に CLOUDFLARE_API_TOKEN がありません}"
: "${CLOUDFLARE_ACCOUNT_ID:?.env に CLOUDFLARE_ACCOUNT_ID がありません}"
: "${CLOUDFLARE_PAGES_PROJECT:?.env に CLOUDFLARE_PAGES_PROJECT がありません}"

# 1) 組み立て: 公開サイトをルートへ、アドバンスドを app/ 配下へ丸ごとコピーする。
#    アドバンスドは vite base=/app/ で組んでいるので (frontend/vite.config.ts)、
#    assets/data/pwa への参照はそのまま /app/… に解決される。
rm -rf "$COMBINED"
mkdir -p "$COMBINED/app"
cp -R "$PUBLIC_DIST/." "$COMBINED/"
cp -R "$MIRROR_DIST/." "$COMBINED/app/"

# 2) _redirects: 公開サイトの規則 (build_public_site.sh が書いた /news 系) に、
#    アドバンスド (/app) の SPA フォールバックを追記する。より具体的な規則を
#    先に書く (重複はしないが、順序の意図を残す)。
cat >> "$COMBINED/_redirects" <<'REDIRECTS'
/app/*    /app/index.html   200
/app      /app/index.html   200
REDIRECTS

# 3) _headers は公開サイトのものをそのまま使う (cp -R で既に乗っている)。
#    アドバンスドに追加のヘッダは無い。

# 4) ファイル数の上限 (Pages は 1 デプロイ 20,000 ファイルまで)。
FILE_COUNT="$(cd "$COMBINED" && find . -type f | wc -l | tr -d ' ')"
if [ "$FILE_COUNT" -ge "$MAX_FILES" ]; then
  echo "配信物が ${FILE_COUNT} ファイル (上限 ${MAX_FILES}) に達しています。配信しません" >&2
  exit 1
fi

# 5) 変更なしなら配信しない (公開サイト・アドバンスドのどちらも変わっていなければ
#    combined のハッシュも変わらない)。
DIGEST="$(cd "$COMBINED" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256 | cut -d' ' -f1)"
if [ "${1:-}" != "--force" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$DIGEST" ]; then
  echo "変更なし (sha256 ${DIGEST:0:12}) — 配信しない"
  exit 0
fi

npx --yes wrangler@latest pages deploy "$COMBINED" \
  --project-name "$CLOUDFLARE_PAGES_PROJECT" \
  --branch main \
  --commit-dirty=true

echo "$DIGEST" > "$STAMP"
echo "配信しました (${FILE_COUNT} ファイル, sha256 ${DIGEST:0:12})"
