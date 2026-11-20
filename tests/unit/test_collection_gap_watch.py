"""収集の空白の見張り (2026-11-20、11-19 監査 D2 P1-7)。

ホストのスリープで収集が最大 42 時間止まり、誰も気付かなかった。スリープ中は見張りも
止まるので、復帰した後の日次ハートビートで「直近 24h に 3h 以上の空白があったか」と
7 日の最大の空白を出す (状態から導くので、復帰後に必ず拾える)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.ui.services.collection_gap_watch import format_gap_line, max_gap_hours

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _hourly(start: datetime, n: int) -> list[datetime]:
    return [start + timedelta(hours=i) for i in range(n)]


def test_max_gap_counts_edges_of_the_window() -> None:
    runs = _hourly(NOW - timedelta(hours=10), 3)  # 最後の run は 8h 前
    assert max_gap_hours(runs, start=NOW - timedelta(hours=10), end=NOW) == 8.0


def test_no_runs_means_whole_window_is_a_gap() -> None:
    assert max_gap_hours([], start=NOW - timedelta(hours=24), end=NOW) == 24.0


def test_line_warns_on_recent_gap() -> None:
    runs = _hourly(NOW - timedelta(hours=24), 10) + _hourly(NOW - timedelta(hours=5), 5)
    line = format_gap_line(runs, now=NOW)
    assert line.startswith("⚠️") and "24h" in line


def test_line_quiet_when_hourly() -> None:
    runs = _hourly(NOW - timedelta(days=7), 7 * 24)
    line = format_gap_line(runs, now=NOW)
    assert "⚠️" not in line
