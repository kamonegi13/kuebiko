"""DETECT-JUDGE の R18 (閉包) 純粋部分。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "gpu_window"))
import detect_judge_core as jc  # noqa: E402
import detect_r18_core as rc  # noqa: E402


def _units() -> dict[str, jc.Unit]:
    return {
        "u1": jc.Unit("u1", "w", ("a",), "c1", ("current",)),
        "u2": jc.Unit("u2", "w", ("b", "c"), "c2", ("ship",)),
        "u3": jc.Unit("u3", "w", ("d",), "c3", ("ship",)),
    }


def test_hub_cut_drops_big_and_empty_fields() -> None:
    vals = {"a": frozenset({"1", "2", "3"}), "b": frozenset(set("12345")), "c": frozenset()}
    assert set(rc.apply_hub_cut(vals)) == {"a"}


def test_id_tokens_keeps_raw_and_stripped() -> None:
    toks = rc.id_tokens('x "rss:https://kuebiko.example/?p=1." y grok:abc:3#99')
    assert "rss:https://kuebiko.example/?p=1." in toks
    assert "rss:https://kuebiko.example/?p=1" in toks
    assert "grok:abc:3#99" in toks


def test_contaminated_units_share_component_with_train() -> None:
    comp = {"a": "X", "t": "X", "b": "Y", "c": "Z", "d": "W"}
    assert rc.contaminated_units(_units(), comp, frozenset({"t"})) == {"u1"}
    comp2 = {**comp, "c": "X"}
    assert rc.contaminated_units(_units(), comp2, frozenset({"t"})) == {"u1", "u2"}


def test_unit_without_component_entry_is_not_contaminated() -> None:
    assert rc.contaminated_units(_units(), {}, frozenset({"zzz"})) == set()


def test_count_by_stratum() -> None:
    assert rc.count_by_stratum(_units(), {"u1", "u2", "u3"}) == {"current": 1, "ship": 2}
