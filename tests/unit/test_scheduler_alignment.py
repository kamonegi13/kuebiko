"""interval の clock-aligned + offset 起点計算のテスト (2026-08-15 バグ修正)。

runtime reschedule 経路が offset_minutes を捨てて :00 aligned に丸めるバグの
回帰固定。now を注入して時限依存を排除する ([[時限テスト解消]] の確立パターン)。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.scheduler.scheduler import _aligned_start_with_offset, _next_aligned_time

_TZ = "Asia/Tokyo"


def _jst(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 15, hour, minute, tzinfo=ZoneInfo(_TZ))


class TestNextAlignedTime:
    def test_rounds_up_to_next_hour_boundary(self) -> None:
        assert _next_aligned_time(60, _TZ, now=_jst(22, 56)) == _jst(23, 0)

    def test_on_boundary_returns_next(self) -> None:
        assert _next_aligned_time(60, _TZ, now=_jst(23, 0)) == _jst(0, 0).replace(day=16)


class TestAlignedStartWithOffset:
    def test_no_offset_is_plain_alignment(self) -> None:
        assert _aligned_start_with_offset(60, 0, _TZ, now=_jst(22, 56)) == _jst(23, 0)

    def test_offset_added_to_next_boundary(self) -> None:
        # 22:56 → 次境界 23:00 + 20 分 = 23:20
        assert _aligned_start_with_offset(60, 20, _TZ, now=_jst(22, 56)) == _jst(23, 20)

    def test_uses_previous_slot_when_still_in_future(self) -> None:
        # 23:05 時点: 次境界 00:00 + 20 = 00:20 だが、直前境界の 23:20 がまだ未来
        # → 23:20 を使う (最大 1 interval の取りこぼし防止)
        assert _aligned_start_with_offset(60, 20, _TZ, now=_jst(23, 5)) == _jst(23, 20)

    def test_previous_slot_in_past_falls_through(self) -> None:
        # 23:25 時点: 直前境界の offset スロット 23:20 は過去 → 00:20
        expected = _jst(0, 20).replace(day=16)
        assert _aligned_start_with_offset(60, 20, _TZ, now=_jst(23, 25)) == expected
