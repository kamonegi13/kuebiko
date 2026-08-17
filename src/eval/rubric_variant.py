"""rubric の変種を **DB に保存せず** メモリ上で作る。

評価のために本番のプロンプトを書き換えてはいけない (DB の rubric を保存すると
production が即座に新版を使う)。ここは「もしこのフィールドを外したら」を
オフラインで試すための変種を組み立てる。

判定基準セクションだけでなく **few-shot 例からも同じキーを外す** のが要点。
片方だけ外すと「基準では触れないが例では示している」という中途半端な状態になり、
何を測ったのか分からなくなる。

⚠ **例の書式は変えない** (2026-08-18)。当初は ``json.dumps(indent=2)`` で作り直して
いたが、それでは「測った変種」と「実際に yaml へ書く形」が空白レベルで一致せず、
差が出たときにキー除去の効果か整形の効果か切り分けられない。行単位で削って
原文の体裁 (1 行配列・インデント幅・キー順) を保つ。行操作で正しい JSON を作れない
ときだけ ``json.dumps`` に退避する — **壊れた JSON をプロンプトへ出さないことが優先**。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from src.prompts.rubric_model import SummarizerRubric

# 「行頭の空白 + "キー" :」= トップレベルのプロパティ行。
_KEY_LINE_RE = re.compile(r'^\s*"([^"]+)"\s*:')


def drop_fields(rubric: SummarizerRubric, field_ids: Iterable[str]) -> SummarizerRubric:
    """``field_ids`` を判定基準セクションと few-shot 例の双方から除いた変種を返す。

    元の ``rubric`` は変更しない (評価中に本番の状態を汚さない)。
    例の JSON が壊れている場合はその例をそのまま残す (評価の前提を静かに変えない)。
    """
    drop = set(field_ids)
    if not drop:
        return rubric

    sections = [s for s in rubric.sections if s.field_id not in drop]

    examples = []
    for ex in rubric.examples:
        examples.append(ex.model_copy(update={"json_text": _strip_keys(ex.json_text, drop)}))

    return rubric.model_copy(update={"sections": sections, "examples": examples})


def _strip_keys(json_text: str, drop: set[str]) -> str:
    """出力例の JSON から指定キーを除く。解析できなければ原文のまま返す。"""
    try:
        obj = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return json_text
    if not isinstance(obj, dict):
        return json_text
    if not any(k in obj for k in drop):
        return json_text  # 対象キーが無いなら 1 文字も触らない

    expected = {k: v for k, v in obj.items() if k not in drop}
    trimmed = _remove_key_lines(json_text, drop)
    if trimmed is not None and _parses_to(trimmed, expected):
        return trimmed
    # 行操作で再現できない書き方 (1 行 JSON・文字列中の括弧 等) は整形し直す。
    # 書式は失うが、キー除去という測定の前提だけは必ず守る。
    return json.dumps(expected, ensure_ascii=False, indent=2)


def _parses_to(json_text: str, expected: dict[str, Any]) -> bool:
    try:
        return bool(json.loads(json_text) == expected)
    except (json.JSONDecodeError, TypeError):
        return False


def _remove_key_lines(json_text: str, drop: set[str]) -> str | None:
    """トップレベルの対象キーの行 (複数行の値ならその全体) を落とす。

    括弧の数え上げは文字列リテラル中の括弧を区別しないため、結果は必ず
    呼び出し側で ``_parses_to`` により検算すること。
    """
    lines = json_text.splitlines()
    kept: list[str] = []
    depth = 0
    skip_until_depth: int | None = None

    for line in lines:
        delta = _bracket_delta(line)
        if skip_until_depth is not None:
            depth += delta
            if depth <= skip_until_depth:
                skip_until_depth = None
            continue
        match = _KEY_LINE_RE.match(line)
        if depth == 1 and match and match.group(1) in drop:
            start_depth = depth
            depth += delta
            if depth > start_depth:  # 値が複数行 (object / array) なら閉じるまで飛ばす
                skip_until_depth = start_depth
            continue
        depth += delta
        kept.append(line)

    if not kept:
        return None
    _drop_dangling_comma(kept)
    return "\n".join(kept)


def _bracket_delta(line: str) -> int:
    return (line.count("{") + line.count("[")) - (line.count("}") + line.count("]"))


def _drop_dangling_comma(lines: list[str]) -> None:
    """最終プロパティ行の末尾カンマを落とす (対象キーが末尾だった場合に生じる)。"""
    seen_closing = False
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].strip():
            continue
        if not seen_closing:
            seen_closing = True  # 最後の非空行 = 全体の閉じ括弧
            continue
        stripped = lines[i].rstrip()
        if stripped.endswith(","):
            lines[i] = stripped[:-1]
        return
