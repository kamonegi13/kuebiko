"""予測較正の外部会計 (F′: 説明責任の反転)。

synthesis は予測 (indicators) を出し、その発火も自分で申告する (hit)。さらに確度も
自己申告する。**自己申告だけが並ぶ場に、決定論の会計を併記する**のが本モジュール。

「当たったか」は正解が無いので判定しない。判定するのは **自己申告どうしの整合** —
「高確度と申告した状況の予測は、低確度より多く発火したか」。情報を持たない (順序が
崩れる/差が CI 内) なら、確度表示は装飾でしかない。

実測 (2026-08-22 導入時): low 60.6% (n=284) > high 50.0% (n=250) > moderate 45.4%
(n=207) — **順序が逆転**しており、確度は自分の発火率を予測できていなかった。
"""

from __future__ import annotations

import pytest

from src.assessment.forecast_calibration import (
    CalibrationCell,
    is_ordering_informative,
    wilson_interval,
)


class TestWilson:
    def test_zero_sample_is_full_range(self) -> None:
        lo, hi = wilson_interval(0, 0)
        assert (lo, hi) == (0.0, 1.0)

    def test_interval_brackets_the_point_estimate(self) -> None:
        lo, hi = wilson_interval(50, 100)
        assert lo < 0.5 < hi

    def test_larger_sample_narrows_the_interval(self) -> None:
        narrow = wilson_interval(500, 1000)
        wide = wilson_interval(5, 10)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


class TestOrdering:
    @staticmethod
    def _cell(conf: str, hit: int, n: int) -> CalibrationCell:
        lo, hi = wilson_interval(hit, n)
        return CalibrationCell(confidence=conf, scored=n, hit=hit, lo=lo, hi=hi)

    def test_monotonic_and_separated_is_informative(self) -> None:
        cells = [
            self._cell("high", 900, 1000),
            self._cell("moderate", 500, 1000),
            self._cell("low", 100, 1000),
        ]
        assert is_ordering_informative(cells) is True

    def test_inverted_ordering_is_not_informative(self) -> None:
        # 実測された形 (low > high)
        cells = [
            self._cell("high", 125, 250),
            self._cell("moderate", 94, 207),
            self._cell("low", 172, 284),
        ]
        assert is_ordering_informative(cells) is False

    def test_overlapping_intervals_are_not_informative(self) -> None:
        # 順序は正しいが CI が重なる = 差を主張できない
        cells = [
            self._cell("high", 11, 20),
            self._cell("moderate", 10, 20),
            self._cell("low", 9, 20),
        ]
        assert is_ordering_informative(cells) is False

    def test_missing_tier_is_not_informative(self) -> None:
        assert is_ordering_informative([self._cell("high", 9, 10)]) is False

    @pytest.mark.parametrize("n", [0, 5])
    def test_small_samples_are_not_informative(self, n: int) -> None:
        cells = [self._cell(c, n, n) for c in ("high", "moderate", "low")]
        assert is_ordering_informative(cells) is False
