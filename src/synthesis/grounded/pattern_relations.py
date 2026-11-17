"""事案どうしの「つながり」の関門 (決定論)。設計: docs/synthesis_pattern_redesign.md §6 3 回目。

モデルが {事案 id の組, 共通点の語 (shared), 文} を提案する。採用の条件:
1. 事案 id が 2 つとも既知で別 (C1… はプロンプトで渡した id)。
2. ``shared`` (NFKC・小文字・空白除去で 2 字以上、一般語でない) が、両事案の claim・題・
   根拠の抜粋に在る。
3. 文が ``shared`` を含み、通常の関門 (禁止語・長さ・委託先等の語・固有名詞/数値の裏付け) を、
   その 2 事案の範囲で通る。
同じ組は 1 回だけ、採用は ``RELATIONS_MAX`` 件まで。落とした理由は件数で残す。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.synthesis.grounded.pattern_case import PatternCase
from src.synthesis.grounded.pattern_gates import COUNT_KEYS, AcceptedRelation, gate_field
from src.synthesis.grounded.patterns import PatternPlan

RELATIONS_MAX = 3
RELATION_SHARED_MIN_CHARS = 2
RELATION_MAX_SENTENCES = 2
#: 共通点として意味が無い一般語 (どの侵害・攻撃の記事にも出る)
GENERIC_SHARED: frozenset[str] = frozenset(
    {
        "日本",
        "海外",
        "侵害",
        "漏えい",
        "漏洩",
        "攻撃",
        "情報",
        "不正",
        "不正アクセス",
        "被害",
        "個人情報",
    }
)
_PAIR = 2


@dataclass(frozen=True)
class RelationStats:
    proposed: int = 0
    accepted: int = 0
    rejected_ids: int = 0
    rejected_shared: int = 0
    rejected_text: int = 0


def _norm(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _pool(case: PatternCase, excerpts: Mapping[str, Sequence[str]]) -> str:
    parts = [*case.claims, case.title]
    parts += [x for jid in case.judgment_ids for x in excerpts.get(jid, ())]
    return _norm("\n".join(parts))


def _usable_shared(shared: str, pools: Sequence[str]) -> str:
    key = _norm(shared)
    if len(key) < RELATION_SHARED_MIN_CHARS or key in {_norm(g) for g in GENERIC_SHARED}:
        return ""
    return key if all(key in p for p in pools) else ""


def gate_relations(
    items: Sequence[tuple[Sequence[str], str, str]],
    plan: PatternPlan,
    excerpts: Mapping[str, Sequence[str]],
) -> tuple[tuple[AcceptedRelation, ...], RelationStats]:
    """(case_ids, shared, text) の提案を関門に通す。"""
    by_id = {c.case_id: c for c in plan.all_cases()}
    accepted: list[AcceptedRelation] = []
    seen: set[frozenset[str]] = set()
    rejected = {"ids": 0, "shared": 0, "text": 0}
    for ids, shared, text in items:
        pair = frozenset(ids)
        if len(ids) != _PAIR or len(pair) != _PAIR or not pair <= by_id.keys() or pair in seen:
            rejected["ids"] += 1
            continue
        a, b = (by_id[i] for i in ids)
        key = _usable_shared(shared, [_pool(a, excerpts), _pool(b, excerpts)])
        if not key or key not in _norm(text):
            rejected["shared"] += 1
            continue
        pool = [x for c in (a, b) for jid in c.judgment_ids for x in excerpts.get(jid, ())]
        counts = dict.fromkeys(COUNT_KEYS, 0)
        gated = gate_field(
            text,
            cases=[a, b],
            patterns=[],
            max_sentences=RELATION_MAX_SENTENCES,
            counts=counts,
            extra_claims=pool,
        )
        if gated is None or key not in _norm(gated):
            rejected["text"] += 1
            continue
        seen.add(pair)
        accepted.append(AcceptedRelation((ids[0], ids[1]), shared.strip(), gated))
        if len(accepted) >= RELATIONS_MAX:
            break
    stats = RelationStats(
        proposed=len(items),
        accepted=len(accepted),
        rejected_ids=rejected["ids"],
        rejected_shared=rejected["shared"],
        rejected_text=rejected["text"],
    )
    return tuple(accepted), stats
