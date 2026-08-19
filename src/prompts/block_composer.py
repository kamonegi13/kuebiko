"""block 方式の合成: skeleton (code 所有) + blocks (DB 所有) のマーカー置換。

skeleton はマーカー行 ``{# BLOCK: <id> #}`` を含む .j2。マーカーは **Jinja コメント**
なので、万一置換されずに残っても render 出力には現れない (fail-safe)。

不変量: **seed の blocks で合成した結果は legacy .j2 と byte 一致** (golden test)。
差分分類機は要らない — 逸脱は UI 編集でのみ生まれ、版履歴に残る。
"""

from __future__ import annotations

import re

from src.prompts.rubric_model import SummarizerRubric

_MARKER_RE = re.compile(r"^\{#\s*BLOCK:\s*([a-z0-9_]+)\s*#\}$", re.MULTILINE)


def skeleton_slots(skeleton_text: str) -> list[str]:
    """skeleton のマーカー id を出現順で返す。"""
    return _MARKER_RE.findall(skeleton_text)


def validate_blocks(skeleton_text: str, rubric: SummarizerRubric) -> list[str]:
    """slot と block の 1:1 を検証し、エラー文の list を返す (空 = OK)。

    - skeleton の slot に対応する block が無い → 合成すると段落が欠落する (エラー)
    - blocks に skeleton に無い id がある → 書いても捨てられる write-only (エラー)
    - block 方式は few-shot examples を使わない (出力例は該当 block の本文に含める)
    """
    slots = skeleton_slots(skeleton_text)
    slot_set = set(slots)
    block_ids = [s.field_id for s in rubric.sections]
    errors: list[str] = []
    for duplicate in {b for b in block_ids if block_ids.count(b) > 1}:
        errors.append(f"block id が重複しています: {duplicate}")
    for missing in [s for s in slots if s not in set(block_ids)]:
        errors.append(f"skeleton の slot に対応する block がありません: {missing}")
    for unknown in [b for b in block_ids if b not in slot_set]:
        errors.append(f"skeleton に無い block です (書いても出力されません): {unknown}")
    if rubric.examples:
        errors.append("block 方式は examples を使いません (出力例は該当 block の本文へ)")
    return errors


def compose_blocks(skeleton_text: str, rubric: SummarizerRubric) -> str:
    """マーカー行を block 本文で置換した合成テンプレート原文を返す。

    置換は行単位 (マーカー行そのものが body に変わる)。body 末尾の改行有無は
    seed 抽出時に byte 一致で固定されるため、ここでは加工しない (verbatim)。
    """
    bodies = {s.field_id: s.body for s in rubric.sections}

    def _replace(match: re.Match[str]) -> str:
        return bodies.get(match.group(1), match.group(0))

    return _MARKER_RE.sub(_replace, skeleton_text)
