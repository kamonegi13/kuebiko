"""導いた線を日次の観測の写し (``event_edge_records``) の行に直す純関数 (2026-11-02 GRAPH-G0)。

導出 (``eventnews/relations.py``・焦点の照合) が SSoT で、ここはその写しの形を決めるだけ。
DB にも LLM にも触らない。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from src.eventnews.relations import DerivedRelation
from src.graph.edge_tiers import (
    EDGE_INCIDENT,
    EDGE_LINEAGE,
    EDGE_SAME_ACTOR,
    edge_type_of,
    tier_for_edge_type,
)

#: 導出 (relations.py) の規則の版。導き方を変えたら上げる
RELATION_RULE_VERSION = "2026-09-27.1"
#: 線の出どころ
SOURCE_DERIVED = "derived"
SOURCE_VETO = "veto"
SOURCE_DECISION = "decision"


@dataclass(frozen=True)
class EdgeRecord:
    """写しの 1 行 (導出の線 1 本)。"""

    derived_at: str
    edge_key: str
    item_a: str
    item_b: str
    root_a: str
    root_b: str
    edge_type: str
    rel_type: str
    source: str
    basis_json: str
    score: float | None
    rule_version: str
    model_sha: str
    tier: str


def make_edge_key(item_a: str, item_b: str, edge_type: str, rule_version: str) -> str:
    lo, hi = sorted((item_a, item_b))
    return f"{lo}|{hi}|{edge_type}|{rule_version}"


def _score(rel: DerivedRelation, edge_type: str) -> float | None:
    key = "cos" if edge_type == EDGE_SAME_ACTOR else "p"
    raw = rel.extra.get(key)
    try:
        return None if raw is None else float(raw)
    except ValueError:
        return None


def records_from_relations(
    rels: Iterable[DerivedRelation],
    *,
    derived_at: str,
    model_sha: str,
    rule_version: str = RELATION_RULE_VERSION,
) -> list[EdgeRecord]:
    """導いた関係 (事象ごとの索引を平らにしたもの、両端で重複しうる) → 行。キーで重複を畳む。

    生きた事象どうしの線なので根は事象 id 自身。
    """
    out: dict[str, EdgeRecord] = {}
    for r in rels:
        etype = edge_type_of(r.rel_type)
        key = make_edge_key(r.a, r.b, etype, rule_version)
        if key in out:
            continue
        out[key] = EdgeRecord(
            derived_at=derived_at,
            edge_key=key,
            item_a=r.a,
            item_b=r.b,
            root_a=r.a,
            root_b=r.b,
            edge_type=etype,
            rel_type=r.rel_type,
            source=SOURCE_DERIVED,
            basis_json=json.dumps(list(r.basis), ensure_ascii=False),
            score=_score(r, etype),
            rule_version=rule_version,
            model_sha=model_sha,
            tier=tier_for_edge_type(etype),
        )
    return list(out.values())


def records_from_lineage(
    pairs: Iterable[tuple[str, str, str]],
    *,
    derived_at: str,
    roots: Mapping[str, str],
    rule_version: str,
) -> list[EdgeRecord]:
    """焦点の lineage の組 ``(item_a, item_b, source)`` → 行。

    ``roots`` は事象 → 根 (無ければ自身)。
    """
    out: dict[str, EdgeRecord] = {}
    for a, b, source in pairs:
        key = make_edge_key(a, b, EDGE_LINEAGE, rule_version)
        if key in out:
            continue
        out[key] = EdgeRecord(
            derived_at=derived_at,
            edge_key=key,
            item_a=min(a, b),
            item_b=max(a, b),
            root_a=roots.get(min(a, b), min(a, b)),
            root_b=roots.get(max(a, b), max(a, b)),
            edge_type=EDGE_LINEAGE,
            rel_type=EDGE_LINEAGE,
            source=source,
            basis_json="[]",
            score=None,
            rule_version=rule_version,
            model_sha="",
            tier=tier_for_edge_type(EDGE_LINEAGE),
        )
    return list(out.values())


__all__ = [
    "EDGE_INCIDENT",
    "RELATION_RULE_VERSION",
    "SOURCE_DECISION",
    "SOURCE_DERIVED",
    "SOURCE_VETO",
    "EdgeRecord",
    "make_edge_key",
    "records_from_lineage",
    "records_from_relations",
]
