"""時刻の語彙 (event_time.TIME_VOCAB) と共有の窓関数の固定。"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from src.storage.event_time import (
    TIME_VOCAB,
    anchor_ts,
    classify_event_date,
    coverage_counts,
    window_clause,
)
from src.ui.services.geo_cyber_map import _win_frag

_TS = Path(__file__).resolve().parents[2] / "frontend/src/utils/timeVocab.ts"


def test_frontend_vocab_matches_python() -> None:
    src = _TS.read_text(encoding="utf-8")
    block = src.split("export const TIME_VOCAB = {")[1].split("} as const")[0]
    ts_vocab = dict(re.findall(r'(\w+): "([^"]+)"', block))
    assert ts_vocab == TIME_VOCAB


def test_classify_event_date_states() -> None:
    assert classify_event_date(None) == "unknown"
    assert classify_event_date("") == "unknown"
    assert classify_event_date("2026-01-02", "reported") == "reported_only"
    assert classify_event_date("2026-01-02", "detected") == "occurred"
    assert classify_event_date("2026-01-02", None) == "occurred"
    assert classify_event_date("2026-01-01", "occurred", "month") == "month_only"


def test_coverage_counts_moves_reported_only_to_unknown() -> None:
    cov = coverage_counts(["occurred", "reported_only", "unknown", "month_only"], 3)
    assert cov["occurred"] == cov["dated"] == 1
    assert cov["unknown"] == 2 and cov["reported_only"] == 1
    assert cov["month_only"] == 1 and cov["total"] == 4 and cov["event_in_window"] == 3


def test_anchor_ts_caps_at_ingest() -> None:
    assert anchor_ts("2026-01-01", "2026-01-02") == "2026-01-01"
    assert anchor_ts("2026-01-03", "2026-01-02") == "2026-01-02"
    assert anchor_ts(None, "2026-01-02") == "2026-01-02"


def test_geo_map_uses_shared_window_function() -> None:
    for basis in ("report", "event"):
        assert _win_frag(basis, "a.") == window_clause(basis, "a.")


def test_event_window_excludes_reported_basis() -> None:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE a (id TEXT, event_date TEXT, event_date_basis TEXT)")
    con.executemany(
        "INSERT INTO a VALUES (?,?,?)",
        [
            ("x", "2026-05-01", "detected"),
            ("y", "2026-05-01", "reported"),
            ("z", "2026-05-01", None),
        ],
    )
    rows = con.execute(
        "SELECT id FROM a WHERE 1=1" + window_clause("event"), ("2026-04-01T00:00:00",)
    ).fetchall()
    assert sorted(r[0] for r in rows) == ["x", "z"]
