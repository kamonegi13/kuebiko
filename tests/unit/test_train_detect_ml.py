"""閾値は消化能力の分位で決める (2026-09-17)。"""

from __future__ import annotations

import numpy as np

from scripts.train_detect_ml import choose_threshold


def test_threshold_picks_target_count_per_day() -> None:
    probs = np.array([0.1, 0.9, 0.5, 0.7, 0.3, 0.8])
    # 2 日 × 2 件/日 = 4 件 → 上位 4 件目 (0.5) が閾値
    assert choose_threshold(probs, days=2, target_per_day=2) == 0.5


def test_threshold_falls_back_to_min_when_target_exceeds_population() -> None:
    probs = np.array([0.4, 0.6])
    assert choose_threshold(probs, days=10, target_per_day=6) == 0.4
