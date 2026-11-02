"""導いた線の用途の範囲 (tier) の SSoT (2026-11-02 GRAPH-G0)。

- ``writer``: 書き手 (状況総括の文脈など) に渡してよい
- ``screen``: 画面に出してよい (書き手には渡さない)
- ``check``: 点検専用 (未測定・精度が足りない型。画面・書き手には使わない)

⭐ いまは全型 ``check`` で開始する。E2 (data/eval/graphrag_e1_e2_result_2026_11_02.md) の結果
(same_actor=writer / incident=screen / lineage=check) は GRAPH-G0-TIERS で別途書く。
tier は定数であって表の行ではない。API の表示の経路はここを読み、
``event_edge_records`` の行は読まない。
"""

from __future__ import annotations

from typing import Final

TIER_WRITER: Final = "writer"
TIER_SCREEN: Final = "screen"
TIER_CHECK: Final = "check"
TIERS: Final[tuple[str, ...]] = (TIER_WRITER, TIER_SCREEN, TIER_CHECK)

EDGE_SAME_ACTOR: Final = "same_actor"
EDGE_INCIDENT: Final = "incident"
EDGE_LINEAGE: Final = "lineage"
EDGE_TYPES: Final[tuple[str, ...]] = (EDGE_SAME_ACTOR, EDGE_INCIDENT, EDGE_LINEAGE)

#: 導出の元の種類 (relations.py の rel_type) → 線の型。同じ出来事の系統 (follow_up / side /
#: contains / incident) は ``incident`` にまとめる。ここに無い種類は未測定 = check
_REL_TYPE_TO_EDGE: Final[dict[str, str]] = {
    "same_actor": EDGE_SAME_ACTOR,
    "incident": EDGE_INCIDENT,
    "follow_up": EDGE_INCIDENT,
    "side": EDGE_INCIDENT,
    "contains": EDGE_INCIDENT,
    "lineage": EDGE_LINEAGE,
}

#: 線の型 → tier。GRAPH-G0-TIERS で E2 の結果に更新する
EDGE_TIERS: Final[dict[str, str]] = {
    EDGE_SAME_ACTOR: TIER_CHECK,
    EDGE_INCIDENT: TIER_CHECK,
    EDGE_LINEAGE: TIER_CHECK,
}


def edge_type_of(rel_type: str) -> str:
    """導出の種類 → 線の型。未知の種類はそのまま返す (tier は check に倒れる)。"""
    return _REL_TYPE_TO_EDGE.get(rel_type, rel_type)


def tier_for_edge_type(edge_type: str) -> str:
    """線の型の tier。未測定の型は check。"""
    return EDGE_TIERS.get(edge_type, TIER_CHECK)


def tier_for_rel_type(rel_type: str) -> str:
    """導出の種類 (API の rel_type) の tier。"""
    return tier_for_edge_type(edge_type_of(rel_type))
