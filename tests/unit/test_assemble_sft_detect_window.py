"""detect の評価窓を教師から除外する (2026-09-24)。

評価窓の日を学習させると、学習後の detect を学習済みの日で測ることになる。
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.assemble_sft_dataset import DETECT_EVAL_WINDOW, _day_range, _load_pairs


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def test_day_range_is_inclusive() -> None:
    days = _day_range("2026-09-10:2026-09-12")
    assert days == frozenset({"2026-09-10", "2026-09-11", "2026-09-12"})


def test_none_disables_the_window() -> None:
    assert _day_range("none") == frozenset()
    assert _day_range("") == frozenset()


def test_default_window_covers_both_judged_periods() -> None:
    days = _day_range(DETECT_EVAL_WINDOW)
    assert "2026-09-10" in days and "2026-09-23" in days
    assert "2026-09-09" not in days and "2026-09-24" not in days


def test_rows_inside_the_window_are_dropped(tmp_path: Path) -> None:
    f = tmp_path / "detect.jsonl"
    _write(
        f,
        [
            {"key": "2026-09-09:cur", "prompt": "p1", "completion": "c1"},
            {"key": "2026-09-15:cur", "prompt": "p2", "completion": "c2"},
            {"day": "2026-09-23", "key": "x", "prompt": "p3", "completion": "c3"},
        ],
    )
    rows = _load_pairs(f, "detect", with_prefix=False, skip_days=_day_range(DETECT_EVAL_WINDOW))
    assert [r["prompt"] for r in rows] == ["p1"]


def test_no_window_keeps_every_row(tmp_path: Path) -> None:
    f = tmp_path / "detect.jsonl"
    _write(f, [{"key": "2026-09-15:cur", "prompt": "p", "completion": "c"}])
    assert len(_load_pairs(f, "detect", with_prefix=False)) == 1
