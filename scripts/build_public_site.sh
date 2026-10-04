#!/usr/bin/env bash
# 公開サイトの配信物を組み立てる (Cloudflare Pages へ上げる 1 ディレクトリを作る)。
#
#   scripts/build_public_site.sh [出力先]
#
# 中身: 公開面だけのビルド + 書き出した JSON + SPA フォールバック + robots.txt。
# **管理 UI は含めない** (公開面専用エントリでビルドする)。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/data/public_site_dist}"
DATA="$ROOT/data/public_site"
BASE="/news"

# 1) データの書き出し
#
#    ⚠ **コンテナ内で実行する**。production の DB は compose の postgres service に
#    あり、ホストから `uv run` すると DATABASE_URL が無いため SQLite の空 DB へ
#    フォールバックして **0 件のサイトを配信する** (2026-08-26 に実際に起きた)。
#    書き出し自体にも件数の下限を置いてあるが、まず環境を揃える。
CONTAINER="${KUEBIKO_CONTAINER:-kuebiko}"
if [ -n "${SKIP_EXPORT:-}" ]; then
  echo "書き出しを省略 (SKIP_EXPORT)"
else
  docker cp "$ROOT/scripts/export_public_site.py" "$CONTAINER:/app/scripts/export_public_site.py" >/dev/null
  docker exec -w /app -e PYTHONPATH=/app "$CONTAINER" \
    /app/.venv/bin/python scripts/export_public_site.py --out /tmp/public_site_export
  rm -rf "$DATA"
  mkdir -p "$(dirname "$DATA")"
  docker cp "$CONTAINER:/tmp/public_site_export" "$DATA" >/dev/null
fi

# 2) 公開面だけのビルド
# 運用画面は静的配信側に無いので、ログインリンクは tunnel 側のホストを指す。
# 実ホストは運用者固有なので .env の OPERATOR_ORIGIN に置く (deploy_public_site.sh が読み込む)
: "${OPERATOR_ORIGIN:?OPERATOR_ORIGIN (運用画面のオリジン、例 https://ops.kuebiko.example) を .env に設定してください}"
# 標準⇄アドバンスドの切替導線が使う写しのオリジン。未設定でも導線が無効表示になるだけ
# なので必須にしない (deploy_public_site.sh が .env から読み込む。空なら無効表示)。
MIRROR_ORIGIN="${MIRROR_ORIGIN:-}"
( cd "$ROOT/frontend" && VITE_PUBLIC_STATIC=1 VITE_PUBLIC_BASE="$BASE" VITE_PUBLIC_DATA="/data" \
    VITE_OPERATOR_ORIGIN="$OPERATOR_ORIGIN" VITE_MIRROR_ORIGIN="$MIRROR_ORIGIN" npx vite build )

# 3) 配信物へまとめる
rm -rf "$OUT"
mkdir -p "$OUT"
cp -R "$ROOT/frontend/dist-public/." "$OUT/"
cp -R "$DATA" "$OUT/data"
# 4) SPA フォールバックと検索避け
#    /news/<id> のような深いリンクは実ファイルが無いので、同じ HTML を返す。
#
#    ⚠ **`/` からのリダイレクトを置かない**。Cloudflare Pages は HTML への
#    rewrite 先を拡張子なしへ正規化する (実測: `/news → /app.html 200` が
#    `/news → /app` の 308 になった)。`/ → /news` を足すと往復する。
#    ルート直下の index.html をそのまま出し、残りを SPA フォールバックへ回す。
mv "$OUT/public.html" "$OUT/index.html"
cat >| "$OUT/_redirects" <<'REDIRECTS'
/news/*   /index.html   200
/news     /index.html   200
REDIRECTS
# 5) 配布物 (PDF)。URL で直接ダウンロードさせる (画面からの導線は無い)。
#    置き場はリポジトリ外 (data/ は git 管理外)。置き場が無ければ何もしない
python3 "$ROOT/scripts/build_public_downloads.py" \
  --src "${PUBLIC_DOWNLOADS_DIR:-$ROOT/data/public_downloads}" --out "$OUT"
cat >| "$OUT/_headers" <<'HEADERS'
/downloads/*
  Content-Disposition: attachment
  X-Robots-Tag: noindex
HEADERS
cat >| "$OUT/robots.txt" <<'ROBOTS'
# 広く公開する意図はない (組織内の限られた読者に URL を渡す運用)。
User-agent: *
Disallow: /
ROBOTS

echo "配信物: $OUT ($(du -sh "$OUT" | cut -f1))"
