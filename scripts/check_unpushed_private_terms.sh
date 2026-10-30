#!/bin/bash
# push の前に流す: 運用者を識別する語 (実運用のドメイン・帰属の語) が、未 push のコミットの
# 足した行かコミットメッセージに含まれていないかを確かめる。含まれていれば終了コード 1。
# 語は gitignore 済みのローカルファイル config/local/push_guard_terms.txt から読む
# (1 行 1 語、# 始まりと空行は無視、大文字小文字は区別しない。形式は
# scripts/push_guard_terms.example.txt)。出力は件数のみで、語も該当行も表示しない。
# 既に push 済みの履歴は対象外。
#
# 使い方: bash scripts/check_unpushed_private_terms.sh [--list-commits] [remote/branch]   (既定 origin/main)
# 止まったら、--list-commits で該当コミットを hash だけ表示し、まとめ直して該当の文を消してから push する。
# git の pre-push フックにするなら、運用者が自分で .git/hooks/pre-push から呼ぶ。
# 方針: 敵対国の政府・メディア等のドメインは公開してよい (docs/local_overlay.md)。守るのは運用者の識別。
set -u
ROOT="$(git rev-parse --show-toplevel)"
LIST_COMMITS=0
if [ "${1:-}" = "--list-commits" ]; then LIST_COMMITS=1; shift; fi
UPSTREAM="${1:-origin/main}"
TERMS_FILE="${PUSH_GUARD_TERMS_FILE:-$ROOT/config/local/push_guard_terms.txt}"
if [ ! -r "$TERMS_FILE" ]; then
  echo "語の一覧が読めない — 検査できない (push しない)" >&2
  exit 1
fi
PATFILE="$(mktemp)"
trap 'rm -f "$PATFILE"' EXIT
grep -vE '^[[:space:]]*(#|$)' "$TERMS_FILE" >"$PATFILE" || true
if [ ! -s "$PATFILE" ]; then
  echo "語の一覧が空 — 検査できない (push しない)" >&2
  exit 1
fi
commits="$(git rev-list --count "$UPSTREAM..HEAD")"
hit_commits=""
n=0
for c in $(git rev-list "$UPSTREAM..HEAD"); do
  # コミットメッセージ (MSG 行) と追加行 (+ で始まり +++ でない行) だけを見る
  if git show --format='MSG %s%n%b' "$c" | grep -E '^(\+[^+]|MSG )' \
    | grep -qiF -f "$PATFILE"; then
    hit_commits="$hit_commits $(git rev-parse --short "$c")"
    n=$((n + 1))
  fi
done
if [ "$n" != "0" ]; then
  echo "NG: 未 push の $commits コミットのうち $n コミットに運用者を識別する語が含まれる ($UPSTREAM..HEAD)" >&2
  if [ "$LIST_COMMITS" = "1" ]; then echo "commits:$hit_commits" >&2; fi
  exit 1
fi
echo "OK: 未 push の $commits コミットに運用者を識別する語は無い ($UPSTREAM..HEAD)"
