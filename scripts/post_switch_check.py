#!/usr/bin/env python3
"""切替後の本番分布の確認 (P0-07、規律 R6) — SELECT のみ。

切替の時刻から 3h / 24h の窓の件数を、前 14 日の同じ時間帯の分布 (最小・中央・最大) と並べ、
範囲外か中央値から +-50% 超に印を付ける。収集の被覆が 50% 未満の窓は「判定不能」。

使い方:
    uv run python scripts/post_switch_check.py --switch 2026-10-30T10:00:00+09:00 --hours 3
結果は data/eval/post_switch/<日時>.json にも書く。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

JST = ZoneInfo("Asia/Tokyo")
BASELINE_DAYS = 14
MIN_COVERAGE = 0.5
DEVIATION_RATIO = 0.5
# 高脅威の安全網が数える category (src/digest/high_threat_digest.py の述語と同じ)
HIGH_THREAT_CATEGORIES = ("apt", "malware", "incident", "advisory", "vulnerability", "breach")
CHANNELS = ("alert", "vuln", "japan_watch", "watch", "brief")
FETCH_PIPELINE_PREFIX = "direct-rss-fetch"
OUT_DIR = Path("data/eval/post_switch")

# bare GROUP BY は使わない (PG で落ちる)。窓の始端・終端が最後の 2 つの ?。
_POSTED_SQL = (
    "SELECT COUNT(DISTINCT article_id) FROM articles"
    " WHERE status='posted' AND posted_channel=? AND created_at>=? AND created_at<?"
)
_HIGH_THREAT_SQL = (
    "SELECT COUNT(DISTINCT COALESCE(dedup_key, article_id)) FROM articles"
    " WHERE status='posted' AND importance='high'"
    " AND posted_channel NOT IN ('alert','vuln')"
    f" AND category IN ({','.join('?' for _ in HIGH_THREAT_CATEGORIES)})"
    " AND created_at>=? AND created_at<?"
)
_POOL_SQL = (
    "SELECT COUNT(DISTINCT article_id) FROM articles"
    " WHERE status='posted' AND importance IN ('high','medium')"
    " AND created_at>=? AND created_at<?"
)
_SOLO_SQL = (
    "SELECT COUNT(*) FROM event_items e WHERE e.origin='live' AND e.importance='high'"
    " AND e.created_at>=? AND e.created_at<?"
    " AND (SELECT COUNT(*) FROM event_item_members m WHERE m.item_id=e.id)=1"
)
_UPDATES_SQL = "SELECT COUNT(*) FROM event_item_versions WHERE generated_at>=? AND generated_at<?"
_AXES_SQL = "SELECT COUNT(*) FROM article_severity_axes WHERE created_at>=? AND created_at<?"
_KINDS_SQL = "SELECT COUNT(*) FROM article_kinds WHERE created_at>=? AND created_at<?"
_FETCH_HOURS_SQL = (
    "SELECT started_at FROM runs WHERE pipeline LIKE ? AND started_at>=? AND started_at<?"
)

# 指標名 -> (SQL, 窓の前に置く引数)
_METRICS: dict[str, tuple[str, tuple[object, ...]]] = {
    **{f"posted_{ch}": (_POSTED_SQL, (ch,)) for ch in CHANNELS},
    "high_threat_digest": (_HIGH_THREAT_SQL, HIGH_THREAT_CATEGORIES),
    "ledger_pool": (_POOL_SQL, ()),
    "solo_reports": (_SOLO_SQL, ()),
    "event_updates": (_UPDATES_SQL, ()),
    "axes_rows": (_AXES_SQL, ()),
    "kind_rows": (_KINDS_SQL, ()),
}


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def shifted_days(self, days: int) -> Window:
        d = timedelta(days=days)
        return Window(self.start - d, self.end - d)

    @property
    def hours(self) -> int:
        return max(1, round((self.end - self.start).total_seconds() / 3600))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def window_counts(conn: Any, window: Window) -> dict[str, int]:
    """1 つの窓の指標を数える (SELECT のみ)。"""
    bounds = (_iso(window.start), _iso(window.end))
    out: dict[str, int] = {}
    for name, (sql, lead) in _METRICS.items():
        row = conn.execute(sql, (*lead, *bounds)).fetchone()
        out[name] = int(row[0] or 0)
    return out


def _hour_key(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H")
    return str(value)[:13]


def coverage(conn: Any, window: Window) -> float:
    """収集 (direct-rss-fetch) が走った時間数 / 窓の時間数。"""
    rows = conn.execute(
        _FETCH_HOURS_SQL,
        (f"{FETCH_PIPELINE_PREFIX}%", _iso(window.start), _iso(window.end)),
    ).fetchall()
    # started_at は SQLite では ISO 文字列、PG では timestamptz。時間の桁で数える
    hours = {_hour_key(r[0]) for r in rows}
    return min(1.0, len(hours) / window.hours)


def judge(value: int, baseline_values: list[int]) -> tuple[tuple[int, float, int] | None, str]:
    """範囲外か中央値 +-50% 超なら flag。基線が空なら undetermined。"""
    if not baseline_values:
        return None, "undetermined"
    lo, hi = min(baseline_values), max(baseline_values)
    med = float(statistics.median(baseline_values))
    outside = value < lo or value > hi
    far = abs(value - med) > DEVIATION_RATIO * med if med > 0 else value > 0
    return (lo, med, hi), "flag" if (outside or far) else "ok"


def run_check(conn: Any, window: Window) -> dict[str, Any]:
    cov = coverage(conn, window)
    current = window_counts(conn, window)
    series: dict[str, list[int]] = {k: [] for k in current}
    used_days = 0
    for k in range(1, BASELINE_DAYS + 1):
        past = window.shifted_days(k)
        if coverage(conn, past) < MIN_COVERAGE:
            continue  # 収集が欠けた日は基線に入れない
        used_days += 1
        for name, v in window_counts(conn, past).items():
            series[name].append(v)
    undetermined = cov < MIN_COVERAGE
    metrics: list[dict[str, Any]] = []
    for name, value in current.items():
        base, status = judge(value, series[name])
        metrics.append(
            {
                "name": name,
                "value": value,
                "baseline": base,
                "status": "undetermined" if undetermined else status,
            }
        )
    flagged = any(m["status"] == "flag" for m in metrics)
    return {
        "window_start": window.start.astimezone(JST).isoformat(),
        "window_end": window.end.astimezone(JST).isoformat(),
        "coverage": round(cov, 3),
        "baseline_days_used": used_days,
        "verdict": "undetermined" if undetermined else ("flag" if flagged else "ok"),
        "metrics": metrics,
    }


def build_window(switch: datetime, hours: int, now: datetime) -> Window:
    return Window(switch, min(switch + timedelta(hours=hours), now))


def format_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"窓 {report['window_start']} - {report['window_end']} (JST)"
        f" 被覆 {report['coverage']} 基線日数 {report['baseline_days_used']}"
        f" 判定 {report['verdict']}"
    ]
    for m in report["metrics"]:
        b = m["baseline"]
        base = f"min {b[0]} / 中央 {b[1]:g} / max {b[2]}" if b else "基線なし"
        mark = {"flag": " <<印", "undetermined": " (判定不能)"}.get(m["status"], "")
        lines.append(f"  {m['name']:<20} {m['value']:>6}  ({base}){mark}")
    return "\n".join(lines)


def main() -> int:
    from src.storage.db_backend import connect
    from src.storage.run_history import DEFAULT_DB_PATH

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--switch", required=True, help="切替の時刻 (ISO、時差なしは JST)")
    ap.add_argument("--hours", type=int, choices=(3, 24), default=3)
    args = ap.parse_args()
    switch = datetime.fromisoformat(args.switch)
    if switch.tzinfo is None:
        switch = switch.replace(tzinfo=JST)
    window = build_window(switch, args.hours, datetime.now(UTC))
    conn = connect(DEFAULT_DB_PATH)
    try:
        report = run_check(conn, window)
    finally:
        conn.close()
    print(format_report(report))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    (OUT_DIR / f"{stamp}_{args.hours}h.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 1 if report["verdict"] == "flag" else 0


if __name__ == "__main__":
    raise SystemExit(main())
