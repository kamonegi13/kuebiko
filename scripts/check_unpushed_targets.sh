#!/bin/bash
# push の前に流す: ローカル専用の一覧 (config/local/) に載るドメインが、未 push のコミットの
# 足した行かコミットメッセージに含まれていないかを確かめる。含まれていれば終了コード 1
# (一覧の中身は表示せず、件数だけ出す)。既に push 済みの履歴は対象外。
#
# 使い方: bash scripts/check_unpushed_targets.sh [remote/branch]   (既定 origin/main)
# 止まったら、未 push のコミットをまとめ直して (squash) 該当の文を消してから push する。
# git の pre-push フックにするなら、運用者が自分で .git/hooks/pre-push から呼ぶ。
set -u
ROOT="$(git rev-parse --show-toplevel)"
UPSTREAM="${1:-origin/main}"
LIST="$(python3 - "$ROOT" <<'EOF'
import pathlib
import sys

import yaml

root = pathlib.Path(sys.argv[1]) / "config" / "local"
names: set[str] = set()


def walk(node: object) -> None:
    """一覧の形 (文字列・{domain: ...}・{hosts: [...]}) を問わず、ドメインらしい文字列を集める。"""
    if isinstance(node, str):
        s = node.strip().lower().lstrip("*.")
        if "." in s and " " not in s and "/" not in s:
            names.add(s)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in ("domain", "domains", "hosts", "host") or isinstance(v, (list, dict)):
                walk(v)
    elif isinstance(node, list):
        for x in node:
            walk(x)


for f in ("hostile_domains.yaml", "source_reliability.yaml"):
    p = root / f
    if p.exists():
        walk(yaml.safe_load(p.read_text()) or {})
print("\n".join(sorted(names)))
EOF
)"
if [ -z "$LIST" ]; then
  echo "config/local の一覧が読めない — 検査できない (push しない)" >&2
  exit 1
fi
PAT="$(printf '%s\n' "$LIST" | sed 's/\./\\./g' | paste -sd'|' -)"
hits="$(git log -p --format='MSG %s%n%b' "$UPSTREAM..HEAD" \
  | grep -E '^(\+[^+]|MSG )' | grep -oiE "($PAT)" | sort -u | wc -l | tr -d ' ')"
commits="$(git rev-list --count "$UPSTREAM..HEAD")"
if [ "$hits" != "0" ]; then
  echo "NG: 未 push の $commits コミットに、一覧のドメインが $hits 種含まれる ($UPSTREAM..HEAD)" >&2
  exit 1
fi
echo "OK: 未 push の $commits コミットに一覧のドメインは無い ($UPSTREAM..HEAD)"
