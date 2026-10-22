"""統合の判断記録を組み立てる純粋関数 (2026-10-22)。"""

from __future__ import annotations

import json

from src.eventnews.merge import MergeGroup, MergeLink
from src.ui.services.eventnews_merge_decisions import (
    MAX_RECORDED_EDGES,
    SOURCE_FREQUENT,
    SOURCE_ML,
    build_decision_rows,
)


def _cos(a: str, b: str) -> float:
    return 0.5 + 0.01 * (len(a) + len(b))


class TestBuildDecisionRows:
    def test_one_row_per_absorbed_item_with_its_own_edges(self) -> None:
        group = MergeGroup(
            target="ev-a",
            absorbed=("ev-b", "ev-c"),
            links=(
                MergeLink("ev-a", "ev-b", (("a1", "b1"), ("a2", "b2"))),
                MergeLink("ev-b", "ev-c", (("b1", "c1"),), rescued=True),
            ),
        )

        rows = build_decision_rows(
            [group],
            probas={("a1", "b1"): 0.9, ("a2", "b2"): 0.8, ("b1", "c1"): 0.7},
            frequent_pairs=frozenset({("b1", "c1")}),
            cos_of=_cos,
            flags={"cve_gate": True},
            run_kind="hourly",
        )

        by_abs = {r.absorbed_id: r for r in rows}
        assert set(by_abs) == {"ev-b", "ev-c"}
        assert all(r.target_id == "ev-a" for r in rows)
        # ev-b は 2 つの組に関わる (統合先との組と ev-c との組)
        assert by_abs["ev-b"].edge_count == 3
        assert by_abs["ev-b"].rescued is True
        assert by_abs["ev-c"].edge_count == 1
        edges = json.loads(by_abs["ev-c"].edges_json)
        assert edges == [
            {
                "a": "b1",
                "b": "c1",
                "cos": round(_cos("b1", "c1"), 4),
                "proba": 0.7,
                "source": SOURCE_FREQUENT,
            }
        ]
        assert json.loads(by_abs["ev-b"].flags_json) == {"cve_gate": True}
        assert {e["source"] for e in json.loads(by_abs["ev-b"].edges_json)} == {
            SOURCE_ML,
            SOURCE_FREQUENT,
        }

    def test_edges_are_capped_but_the_count_stays_true(self) -> None:
        many = tuple((f"a{i}", f"b{i}") for i in range(MAX_RECORDED_EDGES + 20))
        group = MergeGroup("ev-a", ("ev-b",), (MergeLink("ev-a", "ev-b", many),))

        (row,) = build_decision_rows(
            [group],
            probas={},
            frequent_pairs=frozenset(),
            cos_of=_cos,
            flags={},
            run_kind="manual",
        )

        assert row.edge_count == MAX_RECORDED_EDGES + 20
        assert len(json.loads(row.edges_json)) == MAX_RECORDED_EDGES
        assert row.run_kind == "manual"

    def test_missing_probability_is_recorded_as_null(self) -> None:
        group = MergeGroup("ev-a", ("ev-b",), (MergeLink("ev-a", "ev-b", (("a1", "b1"),)),))

        (row,) = build_decision_rows(
            [group], probas={}, frequent_pairs=frozenset(), cos_of=_cos, flags={}, run_kind="hourly"
        )

        assert json.loads(row.edges_json)[0]["proba"] is None

    def test_a_group_without_links_still_records_the_absorption(self) -> None:
        group = MergeGroup("ev-a", ("ev-b",))

        (row,) = build_decision_rows(
            [group], probas={}, frequent_pairs=frozenset(), cos_of=_cos, flags={}, run_kind="hourly"
        )

        assert (row.target_id, row.absorbed_id, row.edge_count) == ("ev-a", "ev-b", 0)
