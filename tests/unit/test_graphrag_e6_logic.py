"""E6 の純関数 (規則の判定・除外・標本の抜き方) のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime

from scripts.graphrag_eval.e6_logic import (
    SEED,
    Unit,
    control_pairs,
    exclude_units,
    in_window,
    is_c2,
    is_c4,
    is_c5,
    layer_summary,
    overlap_keys,
    sample_units,
    window_start,
)


def _pairs(n: int) -> list[Unit]:
    return [Unit("C10", f"e{i:03d}|f{i:03d}", (f"e{i:03d}", f"f{i:03d}")) for i in range(n)]


def test_sample_is_deterministic_for_same_seed_and_layer() -> None:
    units = _pairs(40)
    a = sample_units(units, 5, SEED, "C10")
    b = sample_units(list(reversed(units)), 5, SEED, "C10")
    assert [u.key for u in a] == [u.key for u in b]
    assert len(a) == 5


def test_sample_differs_by_layer_seed() -> None:
    units = _pairs(40)
    assert [u.key for u in sample_units(units, 5, SEED, "C10")] != [
        u.key for u in sample_units(units, 5, SEED, "control")
    ]


def test_take_all_when_population_not_larger_than_n() -> None:
    units = _pairs(3)
    assert [u.key for u in sample_units(units, 5, SEED, "C6")] == sorted(u.key for u in units)
    summary = layer_summary("C6", 5, 3, 3, {}, 3)
    assert summary["take_all"] and summary["short_of_n"]


def test_sample_uses_each_event_once() -> None:
    hub = [Unit("C10", f"h|x{i}", ("h", f"x{i}")) for i in range(10)]
    picked = sample_units(hub, 5, SEED, "C10")
    assert len(picked) == 1


def test_exclusion_order_and_counts() -> None:
    units = [
        Unit("C10", "a|b", ("a", "b")),
        Unit("C10", "a|c", ("a", "c")),
        Unit("C10", "d|e", ("d", "e")),
        Unit("C10", "f|g", ("f", "g")),
        Unit("C10", "h|i", ("h", "i")),
    ]
    kept, dropped = exclude_units(
        units,
        e2_pairs=frozenset({"a|b"}),
        e2_events=frozenset({"a"}),
        guarded_events=frozenset({"d", "a"}),
        guarded_articles=frozenset(),
    )
    assert [u.key for u in kept] == ["f|g", "h|i"]
    assert dropped == {"e2_pair": 1, "e2_event": 1, "guarded": 1}


def test_exclusion_article_unit_via_event_membership_and_guard() -> None:
    units = [
        Unit("C4", "x1", article_ids=("x1",)),
        Unit("C4", "x2", article_ids=("x2",)),
        Unit("C4", "x3", article_ids=("x3",)),
    ]
    kept, dropped = exclude_units(
        units,
        e2_pairs=frozenset(),
        e2_events=frozenset({"ev1"}),
        guarded_events=frozenset(),
        guarded_articles=frozenset({"x2"}),
        article_events={"x1": frozenset({"ev1"})},
    )
    assert [u.key for u in kept] == ["x3"]
    assert dropped == {"e2_pair": 0, "e2_event": 1, "guarded": 1}


def test_rules() -> None:
    assert is_c2({"breach", "exploitation"})
    assert not is_c2({"breach", "other", "roundup"})
    assert is_c4("advisory", {"apt28"}) and not is_c4("breach", {"apt28"})
    assert not is_c4("advisory", set())
    assert is_c5(8, 2) and not is_c5(9, 1) and not is_c5(0, 0)


def test_control_pairs_share_subject_but_disjoint_kinds() -> None:
    events = {
        "e1": (frozenset({"apt1"}), frozenset({"breach"})),
        "e2": (frozenset({"apt1"}), frozenset({"advisory"})),
        "e3": (frozenset({"apt1"}), frozenset({"breach", "stats"})),
        "e4": (frozenset({"apt2"}), frozenset({"advisory"})),
        "e5": (frozenset({"apt1"}), frozenset({"other"})),
    }
    assert control_pairs(events) == ["e1|e2", "e2|e3"]


def test_window_and_overlap() -> None:
    now = datetime(2026, 11, 5, tzinfo=UTC)
    start = window_start(now)
    assert in_window(datetime(2026, 10, 30, tzinfo=UTC), start)
    assert not in_window(datetime(2026, 10, 1, tzinfo=UTC), start) and not in_window(None, start)
    sel = {
        "C1": [Unit("C1", "e1", ("e1",))],
        "C2": [Unit("C2", "e1", ("e1",))],
        "C5": [Unit("C5", "e2", ("e2",))],
    }
    assert overlap_keys(sel) == {"event:e1": ["C1", "C2"]}
