"""scripts/pair_shift_report.py: 3 期の切り分け・帯の集計・KS。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pair_shift_report as psr  # noqa: E402

BOUNDS = ("2026-10-26T00:00:00+00:00", "2026-10-29T00:00:00+00:00")


def _row(at: str, cos: float, llm: int | None, ml: int | None, rule: int = 0) -> psr.PairRow:
    return psr.PairRow(at, cos, llm, ml, rule, {"cos": cos})


def test_assign_period_splits_at_boundaries() -> None:
    assert psr.assign_period("2026-10-25T23:59:59+00:00", BOUNDS) == "before"
    assert psr.assign_period("2026-10-26T00:00:00+00:00", BOUNDS) == "middle"
    assert psr.assign_period("2026-10-29T00:00:00+00:00", BOUNDS) == "after"


def test_cos_band_edges() -> None:
    assert psr.cos_band(0.49) == "0.0-0.5"
    assert psr.cos_band(0.5) == "0.5-0.6"
    assert psr.cos_band(1.0) == "0.9-1.0"


def test_aggregate_excludes_missing_from_rate_denominator() -> None:
    rows = [
        _row("2026-10-27T00:00:00+00:00", 0.75, 1, 1),
        _row("2026-10-27T01:00:00+00:00", 0.72, 0, None),
        _row("2026-10-27T02:00:00+00:00", 0.71, None, None),
    ]
    cell = psr.aggregate(rows, BOUNDS)[("middle", "0.7-0.8")]
    assert cell["n"] == 3
    assert cell["llm_n"] == 2 and cell["llm_same"] == 0.5
    assert cell["ml_n"] == 1 and cell["ml_joined"] == 1.0


def test_ks_statistic() -> None:
    assert psr.ks_statistic([1.0, 2.0], [1.0, 2.0]) == 0.0
    assert psr.ks_statistic([0.0, 0.1], [5.0, 6.0]) == 1.0
    assert psr.ks_statistic([], [1.0]) is None


def test_feature_ks_ranks_shifted_feature_first() -> None:
    rows = [
        psr.PairRow("2026-10-25T00:00:00+00:00", 0.7, 1, 1, 0, {"a": 0.0, "b": 1.0}),
        psr.PairRow("2026-10-25T01:00:00+00:00", 0.7, 1, 1, 0, {"a": 0.1, "b": 2.0}),
        psr.PairRow("2026-10-27T00:00:00+00:00", 0.7, 1, 1, 0, {"a": 9.0, "b": 1.0}),
        psr.PairRow("2026-10-27T01:00:00+00:00", 0.7, 1, 1, 0, {"a": 9.5, "b": 2.0}),
    ]
    ks = psr.feature_ks(rows, BOUNDS, "before", "middle")
    assert next(iter(ks)) == "a" and ks["a"] == 1.0 and ks["b"] == 0.0


def test_parse_row_tolerates_bad_features_json() -> None:
    r = psr.parse_row(["2026-10-27T00:00:00+00:00", 0.8, None, 0, 1, "{broken"])
    assert r.features == {} and r.llm_same is None and r.rule_joined == 1
