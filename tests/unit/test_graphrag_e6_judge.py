from __future__ import annotations

from typing import Any

from scripts.graphrag_eval.e6_judge import (
    ERROR,
    OK,
    UNSURE,
    blind_inputs,
    dedupe,
    rule_decision,
    summarize,
)


def _row(layer: str, key: str) -> dict[str, Any]:
    return {
        "layer": layer,
        "unit": "event",
        "key": key,
        "evidence": {"x": 1},
        "materials": [{"event_id": "ev-1", "headline": "h"}],
    }


def test_blind_inputs_drops_event_id() -> None:
    assert "event_id" not in blind_inputs(_row("C1", "a"))["材料"][0]


def test_dedupe_keeps_first_layer_once() -> None:
    assert len(dedupe([_row("C1", "a"), _row("C2", "a"), _row("C2", "b")])) == 2


def test_rule_decision_thresholds() -> None:
    assert rule_decision(4, 8) == "採用"
    assert rule_decision(1, 8) == "捨てる"
    assert rule_decision(2, 8) == "保留"
    assert rule_decision(3, 4) == "保留(有効n不足)"


def test_summarize_counts_unsure_out_of_valid_n() -> None:
    rows = [_row("C1", str(i)) for i in range(5)]
    gold: dict[str, dict[str, Any]] = {f"event:{i}": {"verdict": ERROR} for i in range(5)}
    gold["event:3"] = {"verdict": OK}
    gold["event:4"] = {"verdict": UNSURE}
    s = summarize(rows, gold, {"C1": 10})
    assert s["layers"]["C1"]["errors"] == 3
    assert s["layers"]["C1"]["valid_n"] == 4
    assert s["layers"]["C1"]["decision"] == "保留(有効n不足)"
    assert s["ship_inspection"] is False
