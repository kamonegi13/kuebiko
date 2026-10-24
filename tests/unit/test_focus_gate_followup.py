"""F2 の継続測定: Wilson 区間と「shadow に戻す」線、抽出の純粋部分 (2026-10-24)。"""

from __future__ import annotations

from src.eventnews.identity.replay import (
    GATE_REVERT_LOWER_BOUND,
    pick_sample,
    should_revert_to_shadow,
    wilson_interval,
)


def test_wilson_interval_matches_known_value() -> None:
    low, high = wilson_interval(26, 31)

    assert round(low, 2) == 0.67
    assert round(high, 2) == 0.93


def test_wilson_interval_of_empty_sample_is_uninformative() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_revert_when_lower_bound_is_below_the_line() -> None:
    # 26/31 は点推定 84% だが区間の下限 67% < 80%
    assert should_revert_to_shadow(26, 31) is True
    assert GATE_REVERT_LOWER_BOUND == 0.80


def test_keep_when_lower_bound_clears_the_line() -> None:
    assert should_revert_to_shadow(95, 100) is False


def test_pick_sample_is_deterministic_and_bounded() -> None:
    keys = [(f"a{i}", f"b{i}") for i in range(40)]

    first = pick_sample(keys, 10, seed=7)
    second = pick_sample(list(reversed(keys)), 10, seed=7)

    assert first == second
    assert len(first) == 10
    assert set(first) <= set(keys)


def test_pick_sample_returns_all_when_fewer_than_requested() -> None:
    keys = [("a", "b"), ("c", "d")]

    assert sorted(pick_sample(keys, 10, seed=1)) == keys
