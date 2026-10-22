"""統合の判断記録の組み立て (純粋関数、2026-10-22)。

``plan_merges`` が返す ``MergeGroup.links`` (結んだ事象の組と記事対) に、ML の確率と cos を
添えて、吸収された事象 1 つにつき 1 行の記録 (``event_merge_decisions``) にする。
後から「なぜ結んだか」を辿れ、``eventnews_merge_undo.undo_merge`` が戻す対象を選ぶ材料になる。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence

from src.eventnews.merge import MergeGroup, MergeLink
from src.storage.repo_merge_decisions import MergeDecisionRow

#: 辺の出どころの SSoT。通常の共有名の辺 / 頻出の名前の共有で足した辺
SOURCE_ML = "ml"
SOURCE_FREQUENT = "frequent"
#: 1 行に残す辺の上限 (ハブでは数百本になる)。本数そのものは ``edge_count`` に真値で残す
MAX_RECORDED_EDGES = 30
#: 実行の種別 (集計の軸)
RUN_KIND_HOURLY = "hourly"
RUN_KIND_MANUAL = "manual"
_COS_DIGITS = 4


def _key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _edge_record(
    pair: tuple[str, str],
    probas: Mapping[tuple[str, str], float],
    frequent: Collection[tuple[str, str]],
    cos_of: Callable[[str, str], float],
) -> dict[str, object]:
    a, b = pair
    key = _key(a, b)
    proba = probas.get(key)
    return {
        "a": a,
        "b": b,
        "cos": round(cos_of(a, b), _COS_DIGITS),
        "proba": None if proba is None else round(float(proba), _COS_DIGITS),
        "source": SOURCE_FREQUENT if key in frequent else SOURCE_ML,
    }


def _links_touching(links: Sequence[MergeLink], item_id: str) -> list[MergeLink]:
    return [lk for lk in links if item_id in (lk.item_a, lk.item_b)]


def build_decision_rows(
    groups: Sequence[MergeGroup],
    *,
    probas: Mapping[tuple[str, str], float],
    frequent_pairs: Collection[tuple[str, str]],
    cos_of: Callable[[str, str], float],
    flags: Mapping[str, object],
    run_kind: str,
) -> list[MergeDecisionRow]:
    """吸収された事象ごとに 1 行。辺はその事象に触れる組の記事対 (cos 降順で上限まで)。"""
    rows: list[MergeDecisionRow] = []
    flags_json = json.dumps(dict(flags), ensure_ascii=False, sort_keys=True)
    for group in groups:
        for absorbed in group.absorbed:
            links = _links_touching(group.links, absorbed)
            pairs = sorted({p for lk in links for p in lk.edges})
            records = [_edge_record(p, probas, frequent_pairs, cos_of) for p in pairs]
            records.sort(key=lambda r: float(r["cos"]), reverse=True)  # type: ignore[arg-type]
            rows.append(
                MergeDecisionRow(
                    target_id=group.target,
                    absorbed_id=absorbed,
                    run_kind=run_kind,
                    edges_json=json.dumps(records[:MAX_RECORDED_EDGES], ensure_ascii=False),
                    edge_count=len(pairs),
                    rescued=any(lk.rescued for lk in links),
                    flags_json=flags_json,
                )
            )
    return rows
