"""近さの経路で統合した組の週次の抜き出し (2026-11-21)。"""

from __future__ import annotations

import json

from src.ui.services.recent_merge_sample import pick, recent_edge


def _edges(*edges: dict[str, object]) -> str:
    return json.dumps(list(edges))


def test_recent_edge_picks_frequent_low_cos_with_highest_proba() -> None:
    edges = _edges(
        {"a": "x", "b": "y", "cos": 0.9, "proba": 0.99, "source": "frequent"},
        {"a": "x", "b": "z", "cos": 0.7, "proba": 0.86, "source": "frequent"},
        {"a": "x", "b": "w", "cos": 0.6, "proba": 0.91, "source": "frequent"},
        {"a": "x", "b": "v", "cos": 0.6, "proba": 0.99, "source": "entity"},
    )

    got = recent_edge(edges)

    assert got is not None and got["b"] == "w"


def test_recent_edge_none_without_recent_path_or_bad_json() -> None:
    assert recent_edge(_edges({"a": "x", "b": "y", "cos": 0.85, "source": "frequent"})) is None
    assert recent_edge("not json") is None


def test_pick_is_deterministic_per_week_and_capped() -> None:
    rows = [
        {
            "id": i,
            "target_id": f"t{i}",
            "absorbed_id": f"a{i}",
            "edges_json": _edges(
                {"a": f"p{i}", "b": f"q{i}", "cos": 0.7, "proba": 0.9, "source": "frequent"}
            ),
        }
        for i in range(30)
    ]
    rows.append({"id": 99, "target_id": "t", "absorbed_id": "a", "edges_json": _edges()})

    first = pick(rows, label="2026-W48")
    again = pick(rows, label="2026-W48")

    assert len(first) == 10
    assert first == again
    assert all(p["decision_id"] != 99 for p in first)
