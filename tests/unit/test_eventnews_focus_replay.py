"""焦点の遡及再現 (scripts/focus_replay.py) の純粋な部品。"""

from __future__ import annotations

from src.eventnews.identity.replay import (
    class_shares,
    final_label,
    members_before,
    size_stats,
    tally_labeled,
)


def test_members_before_keeps_only_rows_joined_before_cutoff() -> None:
    rows = [("a", "2026-10-01T00:00:00+00:00"), ("b", "2026-10-05T00:00:00+00:00")]

    assert members_before(rows, "2026-10-03T00:00:00+00:00") == [("a", "2026-10-01T00:00:00+00:00")]


def test_final_label_prefers_opus_and_falls_back_on_disagreement() -> None:
    assert final_label({"label": "same", "opus1": {"label": "different"}}) == "different"
    assert (
        final_label({"label": "boundary", "opus1": {"label": "same"}, "opus2": {"label": "same"}})
        == "same"
    )
    assert (
        final_label(
            {"label": "boundary", "opus1": {"label": "same"}, "opus2": {"label": "different"}}
        )
        == "boundary"
    )
    assert final_label({"label": "same"}) == "same"


def test_tally_counts_stopped_errors_and_stopped_correct() -> None:
    tally = tally_labeled(
        [
            ("conflict", "different"),
            ("lineage", "boundary"),
            ("weak", "different"),
            ("support", "same"),
            ("conflict", "same"),
        ]
    )

    assert (tally.errors, tally.errors_stopped) == (3, 2)
    assert (tally.correct, tally.correct_stopped) == (2, 1)
    assert tally.by_class["conflict"] == {"other": 1, "same": 1}


def test_class_shares_handles_empty_input() -> None:
    shares = class_shares([])

    assert shares["support"] == (0, 0.0)


def test_size_stats() -> None:
    assert size_stats([]) == (0.0, 0.0, 0)
    median, p90, biggest = size_stats([1, 2, 3, 4, 100])
    assert (median, biggest) == (3.0, 100)
    assert p90 == 100
