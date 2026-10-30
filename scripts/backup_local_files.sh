#!/bin/bash
# git にも DB の日次バックアップにも入らないローカル専用のファイルを、暗号化したディスクイメージ
# (AES-256、macOS 標準の hdiutil) にまとめて退避する。パスワードはその場で入力し、どこにも保存しない。
#
# 使い方: bash scripts/backup_local_files.sh <保存先のディレクトリ>
#   保存先はリポジトリの外 (USB メモリ・別のクラウドの同期フォルダ等) にする。
#   パスワードはパスワード管理ツールに控えておく (このツール・この Mac の中に置かない)。
# 戻し方: 作った .dmg を開く (パスワードを聞かれる) → 中身を同じ相対パスへ写す。
#
# 対象を変えたら、この一覧 (FILES) を直す。DB の中身 (記事・設定の版履歴) は
# backup サイドカーの pg_dump (data/backups/) が別に扱う。
set -euo pipefail
export TZ=Asia/Tokyo

DEST="${1:-}"
if [ -z "$DEST" ]; then
  echo "使い方: bash scripts/backup_local_files.sh <保存先のディレクトリ>" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$(cd "$DEST" && pwd)"
case "$DEST/" in
  "$ROOT"/*) echo "保存先がリポジトリの中にある — 外を指定する" >&2; exit 2 ;;
esac

FILES=(
  ".env"
  "CLAUDE.local.md"
  "prompts/_persona_local.j2"
  "config/local"
)

STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE:?}"' EXIT
found=0
for f in "${FILES[@]}"; do
  if [ -e "$ROOT/$f" ]; then
    mkdir -p "$STAGE/kuebiko-local/$(dirname "$f")"
    cp -R "$ROOT/$f" "$STAGE/kuebiko-local/$f"
    found=$((found + 1))
  else
    echo "無い (飛ばす): $f" >&2
  fi
done
if [ "$found" -eq 0 ]; then
  echo "退避するファイルが 1 つも無い — 中止" >&2
  exit 1
fi

OUT="$DEST/kuebiko-local-$(date '+%Y%m%d-%H%M').dmg"
if [ -e "$OUT" ]; then
  echo "既にある: $OUT — 中止" >&2
  exit 1
fi

read -r -s -p "パスワード: " PW1; echo
read -r -s -p "もう一度: " PW2; echo
if [ -z "$PW1" ] || [ "$PW1" != "$PW2" ]; then
  echo "パスワードが空か一致しない — 中止" >&2
  exit 1
fi
printf '%s' "$PW1" | hdiutil create -quiet -encryption AES-256 -stdinpass \
  -fs APFS -volname kuebiko-local -srcfolder "$STAGE/kuebiko-local" "$OUT"
unset PW1 PW2

echo "作成: $OUT ($found 項目)"
echo "確かめ方: hdiutil attach \"$OUT\" (パスワードを聞かれる) → 中身を見る → hdiutil detach"
