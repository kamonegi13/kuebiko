import random
from datetime import UTC, datetime, timedelta

import pytest

from src.eventnews.relations import DerivedRelation, EventFeatures
from src.graph.nullmodel import (
    NullResult,
    count_by_type,
    null_lift,
    null_results,
    shuffled_events,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _ev(i: int, subjects: frozenset[str]) -> EventFeatures:
    t = T0 + timedelta(days=i)
    return EventFeatures(item_id=f"e{i}", first=t, last=t, subjects=subjects)


def _derive_same_subject(events: list[EventFeatures]) -> list[DerivedRelation]:
    return [
        DerivedRelation(a.item_id, b.item_id, "same_nation", ())
        for i, a in enumerate(events)
        for b in events[i + 1 :]
        if a.subjects & b.subjects
    ]


def test_shuffle_keeps_times_and_multiset_of_values() -> None:
    events = [_ev(i, frozenset({f"a{i}"})) for i in range(6)]
    out = shuffled_events(events, ("subjects",), random.Random(1))
    assert [e.first for e in out] == [e.first for e in events]
    assert sorted(map(sorted, (e.subjects for e in out))) == sorted(
        map(sorted, (e.subjects for e in events))
    )


def test_lift_is_one_when_every_event_shares_the_value() -> None:
    events = [_ev(i, frozenset({"x"})) for i in range(6)]
    n, mean, lift = null_lift(events, _derive_same_subject, rounds=5)["same_nation"]
    assert n == mean == 15
    assert lift == 1.0


def test_lift_is_one_when_pairing_does_not_depend_on_time() -> None:
    # 時期に依らない判定では、並べ替えても本数は変わらない (時期の窓を使う型でだけ lift が動く)
    events = [_ev(0, frozenset({"x"})), _ev(1, frozenset({"x"}))] + [
        _ev(i, frozenset({f"u{i}"})) for i in range(2, 8)
    ]
    n, mean, lift = null_lift(events, _derive_same_subject, rounds=5)["same_nation"]
    assert n == 1
    assert lift == 1.0 and mean == 1.0


def test_zero_mean_gives_none() -> None:
    events = [_ev(i, frozenset({f"u{i}"})) for i in range(4)]
    n, _, lift = null_lift(events, _derive_same_subject, rounds=3)["same_nation"]
    assert n == 0 and lift is None


def test_count_by_type() -> None:
    rels = [
        DerivedRelation("a", "b", "same_nation", ()),
        DerivedRelation("a", "c", "same_target", ()),
    ]
    assert count_by_type(rels) == {"same_nation": 1, "same_target": 1}


def _derive_near_in_time(events: list[EventFeatures]) -> list[DerivedRelation]:
    return [
        DerivedRelation(a.item_id, b.item_id, "same_nation", ())
        for i, a in enumerate(events)
        for b in events[i + 1 :]
        if a.subjects & b.subjects and (b.first - a.last) <= timedelta(days=2)
    ]


def test_time_window_makes_the_null_informative() -> None:
    # 同じ値が時期の近い事象に固まっている実測は、入れ替えると本数が減る (lift > 1)
    events = [_ev(i, frozenset({"x" if i < 4 else f"u{i}"})) for i in range(12)]
    res = null_results(events, _derive_near_in_time, rounds=50)["same_nation"]
    assert res.real > 0
    assert res.lift is not None and res.lift > 1.0
    assert not res.is_chance_level()


def test_timeless_deriver_has_degenerate_null() -> None:
    events = [_ev(i, frozenset({"x" if i % 2 else f"u{i}"})) for i in range(8)]
    res = null_results(events, _derive_same_subject, rounds=10)["same_nation"]
    assert len(set(res.samples)) == 1
    assert res.is_degenerate


def test_intervals_and_validation() -> None:
    res = NullResult(real=10, samples=(2, 4, 6, 8, 10))
    lo, hi = res.null_interval(0.5)
    assert lo == 4 and hi == 8
    assert res.lift == 10 / 6
    with pytest.raises(ValueError):
        null_results([], _derive_same_subject, rounds=0)
