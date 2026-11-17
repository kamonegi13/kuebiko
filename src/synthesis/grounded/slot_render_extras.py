"""SLOT_RENDER の読みやすさの補助 (決定論・純粋関数)。

- ``is_near_duplicate``: 含意が世界側の文の言い換えかを字 2-gram で判定
  (旗 ``SLOT_RENDER_DEDUP``)
- ``compact_pir_section``: SIR ごとに salience 上位 3 件の claim 第 1 文だけを載せる
  (旗 ``SLOT_PIR_COMPACT``)
- ``grouped_chain_text``: 関係データが無いときの連鎖節。共有 SIR・共有根拠記事の件数だけを述べ、
  因果や関係を創作しない (旗 ``SLOT_CHAIN_GROUPING``)
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from itertools import combinations

from src.assessment.salience import rank_judgments
from src.synthesis.grounded.estimate import KeyJudgment

#: 短い方の 2-gram のうち、もう一方に含まれる割合がこれ以上なら言い換えとみなす。
#: 言い換え対 0.50-0.69 / 別内容の含意との対 0.00-0.26 の間 (tests/unit/test_slot_render_extras.py)
DUPLICATE_CONTAINMENT_THRESHOLD = 0.5
#: SIR 1 つあたりに載せる判定の上限
PIR_COMPACT_MAX_ENTRIES = 3
#: claim の第 1 文の上限字数
FIRST_SENTENCE_MAX_CHARS = 120
CHAIN_PIR_GROUPS_MAX = 5

_STRIP = re.compile(r"[\s。、，．,.!！?？「」『』()（）\[\]・:：;；-]")
_CONF_JA = {"high": "高確度", "moderate": "中確度", "low": "低確度"}


def _bigrams(text: str) -> set[str]:
    t = _STRIP.sub("", text)
    return {t[i : i + 2] for i in range(len(t) - 1)}


def is_near_duplicate(a: str, b: str) -> bool:
    """字 2-gram の包含率 (短い方基準) が閾値以上なら True。どちらかが短すぎれば False。"""
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return False
    return len(x & y) / min(len(x), len(y)) >= DUPLICATE_CONTAINMENT_THRESHOLD


def first_sentence(text: str, max_chars: int = FIRST_SENTENCE_MAX_CHARS) -> str:
    """最初の 。 までを返す。長ければ max_chars 手前の最後の 、 で切り、無ければ … を付けて切る。"""
    t = text.strip()
    cut = t.find("。")
    s = t if cut < 0 else t[:cut]
    if len(s) <= max_chars:
        return s
    head = s[:max_chars]
    comma = head.rfind("、")
    return head[:comma] + "…" if comma > 0 else head + "…"


def compact_pir_section(
    judgments: Sequence[KeyJudgment],
    titles: dict[str, str],
    *,
    empty: str,
) -> str:
    by_pir: dict[str, list[KeyJudgment]] = defaultdict(list)
    for j in judgments:
        for pid in j.pir_ids:
            by_pir[pid].append(j)
    lines: list[str] = []
    for pid, js in sorted(by_pir.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        ranked = rank_judgments(tuple(js))
        shown = ranked[:PIR_COMPACT_MAX_ENTRIES]
        body = "、".join(
            f"{first_sentence(j.claim) or j.id} ({_CONF_JA.get(j.confidence, j.confidence)})"
            for j in shown
        )
        rest = len(ranked) - len(shown)
        more = f"、ほか {rest} 件" if rest > 0 else ""
        lines.append(f"- {titles.get(pid, pid)}: {body}{more}")
    return "\n".join(lines) or empty


def grouped_chain_text(judgments: Sequence[KeyJudgment], titles: dict[str, str]) -> str:
    """共有 SIR・共有根拠記事の件数だけの連鎖節。何も共有が無ければ空文字。"""
    by_pir: dict[str, set[str]] = defaultdict(set)
    for j in judgments:
        for pid in j.pir_ids:
            by_pir[pid].add(j.id)
    groups = sorted(
        ((pid, ids) for pid, ids in by_pir.items() if len(ids) >= 2),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )[:CHAIN_PIR_GROUPS_MAX]
    lines = [
        f"同じ SIR「{titles.get(pid, pid)}」に紐づく判定: {len(ids)} 件" for pid, ids in groups
    ]
    shared_pairs = sum(
        1
        for a, b in combinations(judgments, 2)
        if {e.article_id for e in a.evidence} & {e.article_id for e in b.evidence}
    )
    if shared_pairs:
        lines.append(f"同じ記事を根拠に持つ判定の組: {shared_pairs} 組")
    if not lines:
        return ""
    note = "(共有の件数のみ。判定どうしの因果・関係はこの総括では扱っていない)"
    return "\n".join(f"- {line}" for line in lines) + "\n" + note
