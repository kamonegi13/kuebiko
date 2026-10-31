"""scripts/weekly_routing_check.py のテスト (帯の計算・入口の拒否・空ベースライン)。"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "weekly_routing_check.py"
_spec = importlib.util.spec_from_file_location("weekly_routing_check_under_test", _PATH)
assert _spec and _spec.loader
wrc: Any = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = wrc
_spec.loader.exec_module(wrc)


def test_band_is_two_sqrt_n_clamped_at_zero() -> None:
    assert wrc.band_for(100) == (80.0, 120.0)
    assert wrc.band_for(1) == (0.0, 3.0)
    assert wrc.band_for(0) == (0.0, 0.0)


def test_compare_flags_only_channels_outside_band() -> None:
    rows = wrc.compare_counts({"watch": 100, "alert": 25}, {"watch": 125, "alert": 30})
    flagged = {r.channel for r in wrc.outside_channels(rows)}
    assert flagged == {"watch"}  # 100±20 に対し 125 は外、25±10 に対し 30 は内


def test_channel_only_in_one_week_is_listed() -> None:
    rows = wrc.compare_counts({"vuln": 4}, {"japan_watch": 3})
    by = {r.channel: r for r in rows}
    assert by["japan_watch"].is_outside  # ベースライン 0 の帯は 0
    assert not by["vuln"].is_outside  # 4±4 の帯は 0 を含む


def test_band_edges_are_inside() -> None:
    rows = wrc.compare_counts({"watch": 100}, {"watch": 120})
    assert not rows[0].is_outside


def test_axes_i58_is_rejected_and_stored_accepted() -> None:
    with pytest.raises(SystemExit):
        wrc.check_axes("i58")
    assert wrc.check_axes("stored") == "stored"


def test_missing_database_url_fails() -> None:
    with pytest.raises(SystemExit):
        wrc.require_database_url({})
    wrc.require_database_url({"DATABASE_URL": "postgresql://x"})


def test_empty_baseline_fails() -> None:
    with pytest.raises(SystemExit):
        wrc.require_nonempty_baseline({}, "x")
    wrc.require_nonempty_baseline({"watch": 1}, "x")


def test_parse_recover_run_ids_reads_second_column() -> None:
    text = "unit1\t8001\tok\t5\t2026-10-29\nunit2\tnotnum\tok\nbad\nunit3\t8003\n"
    assert wrc.parse_recover_run_ids(text) == frozenset({8001, 8003})


def test_window_is_seven_days_back() -> None:
    start, end = wrc.window_for(datetime(2026, 11, 9, tzinfo=UTC))
    assert (end - start).days == 7


def test_main_rejects_i58_before_touching_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    with pytest.raises(SystemExit):
        wrc.main(["--axes", "i58"])


def test_main_without_database_url_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        wrc.main([])
