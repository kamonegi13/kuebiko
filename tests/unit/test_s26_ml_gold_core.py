"""S26-ML-GOLD の決定論の部分 (prereg docs/replay_prereg/s26_ml_gold.md §2・§4・§7)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scripts.s26_ml_gold import core
from scripts.s26_ml_gold.population import REL_MEMBERS, pick_members


def test_allocate_takes_plan_when_enough() -> None:
    # Arrange
    plan = core.ALLOCATION["detect"]
    # Act
    got = core.allocate({"A": 100, "B": 100, "C": 100}, plan)
    # Assert
    assert got == {"A": 24, "B": 20, "C": 16}


def test_allocate_moves_shortfall_to_next_stratum() -> None:
    got = core.allocate({"V": 13, "K": 144, "S": 461}, core.ALLOCATION["relation"])
    assert got == {"V": 13, "K": 32, "S": 15}
    assert sum(got.values()) == 60


def test_allocate_last_shortfall_wraps_to_first() -> None:
    got = core.allocate({"H": 100, "M": 20, "L": 3}, core.ALLOCATION["sevc"])
    assert got == {"H": 37, "M": 20, "L": 3}


def test_allocate_never_exceeds_population() -> None:
    got = core.allocate({"A": 5, "B": 5, "C": 5}, core.ALLOCATION["detect"])
    assert got == {"A": 5, "B": 5, "C": 5}


def test_draw_is_deterministic_and_order_independent() -> None:
    plan = (("A", 2), ("B", 1))
    pools = {"A": ["x3", "x1", "x2", "x4"], "B": ["y2", "y1"]}
    first = core.draw(pools, {"A": 2, "B": 1}, plan, core.SEED)
    again = core.draw(
        {"A": ["x4", "x2", "x1", "x3"], "B": ["y1", "y2"]}, {"A": 2, "B": 1}, plan, core.SEED
    )
    assert first == again
    assert len(first["A"]) == 2 and len(first["B"]) == 1


def test_weights_are_population_over_sample() -> None:
    assert core.weights({"A": 27, "B": 85}, {"A": 24, "B": 20}) == {"A": 27 / 24, "B": 85 / 20}
    assert core.weights({"A": 3}, {"A": 0}) == {}


@pytest.mark.parametrize(
    ("in_shadow", "importance", "want"),
    [(True, "medium", "A"), (False, "high", "B"), (False, "medium", "C"), (True, "low", None)],
)
def test_detect_stratum(in_shadow: bool, importance: str, want: str | None) -> None:
    assert core.detect_stratum(in_shadow=in_shadow, importance=importance) == want


@pytest.mark.parametrize(("p", "want"), [(0.5, "H"), (0.49, "M"), (0.2, "M"), (0.19, "L")])
def test_sevc_stratum_bounds(p: float, want: str) -> None:
    assert core.sevc_stratum(p) == want


def test_relation_stratum_priority() -> None:
    assert (
        core.relation_stratum(
            shared_victims_or_cves=True, shared_capability=True, shared_subjects=True
        )
        == "V"
    )
    assert (
        core.relation_stratum(
            shared_victims_or_cves=False, shared_capability=True, shared_subjects=True
        )
        == "K"
    )
    assert (
        core.relation_stratum(
            shared_victims_or_cves=False, shared_capability=False, shared_subjects=True
        )
        == "S"
    )
    assert (
        core.relation_stratum(
            shared_victims_or_cves=False, shared_capability=False, shared_subjects=False
        )
        is None
    )


def test_pair_key_is_symmetric() -> None:
    assert core.pair_key("ev-b", "ev-a") == core.pair_key("ev-a", "ev-b") == "ev-a|ev-b"


def test_deep_dive_windows_do_not_overlap_and_end_before_switch() -> None:
    ends = core.DD_WINDOW_ENDS
    assert len(ends) == 7
    for prev, cur in zip(ends, ends[1:], strict=False):
        assert cur - prev == core.DD_LOOKBACK
    assert ends[-1] < datetime(2026, 12, 18, tzinfo=UTC)
    assert ends[0] - core.DD_LOOKBACK >= datetime(2026, 10, 25, 16, tzinfo=UTC)


def test_deep_dive_allocation_totals_60() -> None:
    total = sum(n for w in range(1, 8) for _, n in core.dd_allocation(w))
    assert total == 60
    with pytest.raises(ValueError):
        core.dd_allocation(8)


def test_dd_stratum() -> None:
    assert core.dd_stratum(1) == "I" and core.dd_stratum(60) == "I" and core.dd_stratum(61) == "O"


def test_in_window_is_half_open() -> None:
    assert core.in_window(core.WINDOW_START)
    assert not core.in_window(core.WINDOW_END)
    assert not core.in_window(core.WINDOW_START - timedelta(seconds=1))


def test_contaminated_uses_components() -> None:
    comp = {"t1": "c1", "g1": "c1", "g2": "c2"}
    assert core.contaminated(["g1", "g2", "g3"], comp, ["t1"]) == {"g1"}
    assert core.group_contaminated(["g2", "g1"], {"g1"})
    assert not core.group_contaminated(["g2"], {"g1"})


def test_teacher_index_matches_id_url_and_long_title() -> None:
    idx = core.TeacherIndex("... rss:https://kuebiko.example/a ... 長い見出しの記事です ...")
    assert idx.hits(article_id="rss:https://kuebiko.example/a", url="", title="")
    assert idx.hits(article_id="x:1", url="", title="長い見出しの記事です")
    assert not idx.hits(article_id="x:1", url="", title="短い")  # 8 字未満の見出しは照合しない


def test_url_of() -> None:
    assert core.url_of("rss:https://kuebiko.example/a") == "https://kuebiko.example/a"
    assert core.url_of("grok:abc") == ""


def test_pick_members_prefers_body_then_title_only() -> None:
    arts: dict[str, dict[str, Any]] = {
        "a": {"event_ts": "2026-10-28", "body": "x", "title": "t"},
        "b": {"event_ts": "2026-10-27", "body": "y", "title": "t"},
        "c": {"event_ts": "2026-10-26", "body": "", "title": "leak post"},
        "d": {"event_ts": "2026-10-25", "body": "", "title": ""},
        "e": {"event_ts": "2026-10-29", "body": "z", "title": "t"},
    }
    assert pick_members(["a", "b", "c", "d", "e", "missing"], arts) == ["b", "a", "e"][:REL_MEMBERS]
    assert pick_members(["c", "d"], arts) == ["c"]
    assert pick_members(["d"], arts) == []


def test_jsonl_and_sha_are_stable() -> None:
    assert core.jsonl_text([{"b": 1, "a": 2}]) == '{"a": 2, "b": 1}\n'
    assert (
        core.sha256_text("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
