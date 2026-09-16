"""detect 審判の事象単位畳み込み — 同一事象 3 本なら 1 本開けば正解 (2026-09-17)。"""

from __future__ import annotations

from typing import Any

from scripts.analyze_detect_goldset import EventRow, fold_events, held_out_days, report


def _g(
    stratum: str, *, open_: bool = False, watch: bool = False, imp: int = 1, trk: int = 0
) -> dict[str, Any]:
    return {
        "stratum": stratum,
        "gold_open": open_,
        "watch": watch,
        "importance": imp,
        "trackable": trk,
    }


def test_articles_of_one_event_fold_to_one_row_with_or_and_max() -> None:
    # Arrange: 同一事象の 3 記事 — 1 本だけ審判が開設、現行は審判に出していない記事 a3 を開設
    gold = {"a1": _g("random", imp=1, trk=1), "a2": _g("scoring", open_=True, imp=2, trk=3)}
    event_of = {"a1": "ev1", "a2": "ev1"}
    members = {"ev1": ["a1", "a2", "a3"]}

    # Act
    rows = fold_events(gold, event_of, members, opened_articles={"a3"})

    # Assert
    assert len(rows) == 1
    row = rows[0]
    assert row.event_id == "ev1" and row.n_gold_articles == 2
    assert row.cur_open is True  # メンバー a3 が開設されていれば事象は「現行が開設」
    assert row.scoring_top is True and row.random_only is False
    assert row.gold_open is True and row.importance == 2 and row.trackable == 3
    assert row.verdict == "open"


def test_unmapped_article_is_its_own_event() -> None:
    rows = fold_events({"x": _g("incumbent", watch=True)}, {}, {}, opened_articles={"x"})
    assert rows[0].event_id == "x" and rows[0].cur_open and rows[0].verdict == "watch"


def test_verdict_drop_when_neither_open_nor_watch() -> None:
    row = EventRow("e", 1, False, False, False, True, False, False, 0, 0)
    assert row.verdict == "drop"


def test_held_out_days_counts_both_ends() -> None:
    rows = [{"run_at": "2026-08-16T01:00:00"}, {"run_at": "2026-09-09T23:00:00"}]
    assert held_out_days(rows) == 25
    assert held_out_days([]) == 0


def test_report_extrapolates_missed_events_from_random_layer() -> None:
    rows = [
        EventRow("cur", 1, True, False, True, False, True, False, 2, 2),
        EventRow("r1", 1, False, False, False, True, True, False, 2, 3),
        EventRow("r2", 1, False, False, False, True, False, True, 1, 0),
    ]
    text = report(rows, held_out_days=10, held_out_articles=101)
    assert "事象 3 件" in text
    assert "無作為 (両方非該当)" in text
    # 無作為 2 件中 1 件開設 = 50% × (101 − 現行開設 1) = 50 事象 / 10 日 = 5.0/日
    assert "≈50 事象" in text and "5.0/日" in text
