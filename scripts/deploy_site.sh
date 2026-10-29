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
# ホストの時間帯 (利用者が海外で Mac を現地時間にしていても) に依存させない
export TZ=Asia/Tokyo

# LaunchAgent (mirror) は毎時 :50 に起こされる。launchd の StartCalendarInterval は Mac の
# 時間帯でしか動かないため、「日本時間 2:50 から 3 時間ごと」はここで判断する
# (日本時間の時が 3 で割って 2 余る時だけ走る。それ以外は即終了)。手動実行は対象外。
if [ "${KUEBIKO_SCHEDULED_MIRROR:-0}" = "1" ] && [ "$(( 10#$(date +%H) % 3 ))" != "2" ]; then
  exit 0
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 組み立て (build_public_site.sh は OPERATOR_ORIGIN を要する) より前に .env を読む
set -a && [ -f "$ROOT/.env" ] && . "$ROOT/.env"; set +a
# 公開 (毎時) とアドバンスド (3 時間ごと) の定期配信が重なると、組み立て用の場所を消し合い、
# 片方の組み立て途中の中身を配信しうる。手順全体を 1 本ずつに (mkdir は原子的)。最大 40 分待つ。
LOCK="$ROOT/data/.deploy_site.lock"
# shellcheck source=lib/deploy_lock.sh
. "$ROOT/scripts/lib/deploy_lock.sh"
# 古いロック (持ち主の pid が死んでいる / 2 時間超) は奪う。残留で配信が止まり続けるのを防ぐ。
acquire_lock "$LOCK" 2400 10 || { echo "配信のロックを取れませんでした ($LOCK)" >&2; exit 1; }
trap 'release_lock "$LOCK"' EXIT
PUBLIC_DIST="$ROOT/data/public_site_dist"
MIRROR_DIST="$ROOT/data/mirror_dist"
COMBINED="$ROOT/data/combined_site_dist"
STAMP="$ROOT/data/combined_site_deployed.sha256"
# 配信の成功の印 (成功と「変更なし」の両方で更新。外から鮮度を見る)
OK_STAMP="$ROOT/data/deploy_site_last_ok.json"
WRANGLER_VERSION="4.147.0"
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

: "${CLOUDFLARE_API_TOKEN:?.env に CLOUDFLARE_API_TOKEN がありません}"
: "${CLOUDFLARE_ACCOUNT_ID:?.env に CLOUDFLARE_ACCOUNT_ID がありません}"
: "${CLOUDFLARE_PAGES_PROJECT:?.env に CLOUDFLARE_PAGES_PROJECT がありません}"

# 1) 組み立て: 公開サイトをルートへ、アドバンスドを app/ 配下へ丸ごとコピーする。
#    アドバンスドは vite base=/app/ で組んでいるので (frontend/vite.config.ts)、
#    assets/data/pwa への参照はそのまま /app/… に解決される。
rm -rf "$COMBINED"
mkdir -p "$COMBINED/adv"
cp -R "$PUBLIC_DIST/." "$COMBINED/"
cp -R "$MIRROR_DIST/." "$COMBINED/adv/"
# アドバンスドの静的ファイルは /adv/、画面の URL は /app/…。/app/ の下には実在のファイルを置かない
# (/app/* の書き換えは実在のファイルより先に当たり、置くと資産まで HTML に化ける — 2026-10-05 本番で確認)。

# 2) _redirects: 公開サイトの規則 (build_public_site.sh が書いた /news 系) に、
#    アドバンスド (/app) の SPA フォールバックを追記する。より具体的な規則を
#    先に書く (重複はしないが、順序の意図を残す)。
cat >> "$COMBINED/_redirects" <<'REDIRECTS'
/app/*    /adv/   200
/app      /adv/   200
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
  write_success_stamp "$OK_STAMP" "$SIDE" "$DIGEST" unchanged "$WRANGLER_VERSION"
  exit 0
fi

# wrangler は版を固定する (10-29: @latest の 4.155.0 は x64 の Node 向けの workerd を入れられず、
# 10-22 から配信が止まった。4.147.0 は動作を確認済み)
# 「Deployment complete」の後に終了せず 2 時間近くロックを握った事例がある。時間の上限を付け、
# 完了の行が出ていれば成功として扱う。上限を超えて完了の行も無ければ失敗 (EXIT の trap でロックを解放)。
WRANGLER_TIMEOUT="${WRANGLER_TIMEOUT:-900}"
run_with_timeout "$WRANGLER_TIMEOUT" "Deployment complete" \
  npx --yes "wrangler@${WRANGLER_VERSION}" pages deploy "$COMBINED" \
  --project-name "$CLOUDFLARE_PAGES_PROJECT" \
  --branch main \
  --commit-dirty=true || {
  echo "wrangler が失敗または ${WRANGLER_TIMEOUT} 秒の上限を超えました。成功の印は書きません" >&2
  exit 1
}

echo "$DIGEST" > "$STAMP"
write_success_stamp "$OK_STAMP" "$SIDE" "$DIGEST" deployed "$WRANGLER_VERSION"
echo "配信しました (${FILE_COUNT} ファイル, sha256 ${DIGEST:0:12})"
