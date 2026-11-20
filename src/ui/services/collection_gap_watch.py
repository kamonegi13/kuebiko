"""収集の空白の見張り (2026-11-20、11-19 監査 D2 P1-7)。

ホストのスリープで収集 (hourly-collect) が最大 42 時間止まり、検知が無かった。スリープ中は
この見張りも止まるので、**状態 (job_run_log) から導いて**、復帰後の日次ハートビートで出す。
閾値は事前登録: 直近 24h の中に 3h 以上の空白があれば ⚠ (毎時の段なので 1 回の抜けは 2h)。
表は足さない (ハートビートの 1 行だけ)。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

JOB_ID = "hourly-collect"
GAP_WARN_HOURS = 3.0
RECENT_HOURS = 24
LOOKBACK_DAYS = 7


def max_gap_hours(runs: Sequence[datetime], *, start: datetime, end: datetime) -> float:
    """[start, end] の中で run と run (窓の端を含む) の間の最大の空白 (時間、純粋関数)。"""
    points = sorted([start, *(r for r in runs if start <= r <= end), end])
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(points, points[1:], strict=False)]
    return round(max(gaps, default=0.0), 1)


def format_gap_line(runs: Sequence[datetime], *, now: datetime) -> str:
    recent = max_gap_hours(runs, start=now - timedelta(hours=RECENT_HOURS), end=now)
    week = max_gap_hours(runs, start=now - timedelta(days=LOOKBACK_DAYS), end=now)
    mark = "⚠️" if recent >= GAP_WARN_HOURS else ""
    return f"{mark}収集の空白: 24h 最大 {recent}h / 7 日 最大 {week}h"


def _as_utc(value: object) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def build_collection_gap_line(
    repo: RunHistoryRepository, *, now: datetime | None = None
) -> str | None:
    """ハートビートの 1 行。失敗は None (ハートビート本体は止めない)。"""
    try:
        at = now or datetime.now(UTC)
        since = (at - timedelta(days=LOOKBACK_DAYS)).isoformat()
        with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
            rows = conn.execute(
                "SELECT ran_at FROM job_run_log WHERE job_id = ? AND ran_at >= ?",
                (JOB_ID, since),
            ).fetchall()
        return format_gap_line([_as_utc(r["ran_at"]) for r in rows], now=at)
    except Exception as e:  # noqa: BLE001 — heartbeat 本体を止めない
        _log.warning("collection_gap_line_failed", error_type=type(e).__name__)
        return None


__all__ = ["GAP_WARN_HOURS", "build_collection_gap_line", "format_gap_line", "max_gap_hours"]
