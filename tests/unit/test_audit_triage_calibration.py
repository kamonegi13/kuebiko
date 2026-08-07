"""triage 較正監査スクリプトの判定ロジック (detect_calibration_swings) の unit test。

較正は「カテゴリ内 × 隣接週」で測る (総量分布は構成変化に汚染される —
2026-07-11 P4 監査の結論)。少数標本の週は判定から除外する。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from audit_triage_calibration import WeekCell, detect_calibration_swings  # noqa: E402


def _cell(week: str, high_pct: float, *, n: int = 100, category: str = "vulnerability") -> WeekCell:
    return WeekCell(week=week, category=category, n=n, high_pct=high_pct)


def test_detects_swing_beyond_threshold() -> None:
    cells = [_cell("06-01", 40.0), _cell("06-08", 10.0)]
    warns = detect_calibration_swings(cells, min_n=30, swing_pp=25)
    assert len(warns) == 1
    assert "vulnerability" in warns[0]
    assert "-30pp" in warns[0]


def test_gradual_drift_within_threshold_is_ok() -> None:
    # 週ごとの変化が閾値以内なら (累積で大きくても) WARN しない — 段階的な意図的較正変更を許す
    cells = [_cell("06-01", 40.0), _cell("06-08", 25.0), _cell("06-15", 10.0)]
    assert detect_calibration_swings(cells, min_n=30, swing_pp=25) == []


def test_small_sample_weeks_are_excluded() -> None:
    # n < min_n の週は見かけの振れ (少数標本) なので判定から除外
    cells = [_cell("06-01", 40.0), _cell("06-08", 0.0, n=5), _cell("06-15", 38.0)]
    assert detect_calibration_swings(cells, min_n=30, swing_pp=25) == []


def test_categories_are_independent() -> None:
    cells = [
        _cell("06-01", 40.0, category="malware"),
        _cell("06-08", 10.0, category="breach"),  # 別カテゴリの隣接週は比較しない
    ]
    assert detect_calibration_swings(cells, min_n=30, swing_pp=25) == []
