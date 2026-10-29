"""scripts/post_switch_check.py の集計・印・判定不能 (SQLite fixture)。"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import post_switch_check as psc  # noqa: E402

SWITCH = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
WIN = psc.Window(SWITCH, SWITCH + timedelta(hours=3))


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE articles(article_id TEXT, status TEXT, posted_channel TEXT,
          importance TEXT, category TEXT, dedup_key TEXT, created_at TEXT);
        CREATE TABLE runs(pipeline TEXT, started_at TEXT);
        CREATE TABLE event_items(id TEXT, origin TEXT, importance TEXT, created_at TEXT);
        CREATE TABLE event_item_members(item_id TEXT, article_id TEXT);
        CREATE TABLE event_item_versions(item_id TEXT, generated_at TEXT);
        CREATE TABLE article_severity_axes(article_id TEXT, created_at TEXT);
        CREATE TABLE article_kinds(article_id TEXT, created_at TEXT);
        """
    )
    return c


def _art(c: sqlite3.Connection, i: str, ch: str, imp: str, at: datetime) -> None:
    c.execute(
        "INSERT INTO articles VALUES (?,?,?,?,?,?,?)",
        (i, "posted", ch, imp, "apt", None, at.isoformat()),
    )


def _fetch_runs(c: sqlite3.Connection, window: psc.Window, hours: int) -> None:
    for h in range(hours):
        c.execute(
            "INSERT INTO runs VALUES (?,?)",
            ("direct-rss-fetch", (window.start + timedelta(hours=h)).isoformat()),
        )


def test_window_counts_separates_channels_and_high_threat() -> None:
    c = _db()
    at = SWITCH + timedelta(minutes=10)
    _art(c, "a1", "alert", "high", at)
    _art(c, "a2", "watch", "high", at)
    _art(c, "a3", "watch", "medium", at)
    _art(c, "a4", "watch", "high", SWITCH - timedelta(hours=1))
    counts = psc.window_counts(c, WIN)
    assert counts["posted_alert"] == 1
    assert counts["posted_watch"] == 2
    assert counts["high_threat_digest"] == 1  # alert は除外、watch の high のみ
    assert counts["ledger_pool"] == 3


def test_solo_reports_counts_single_member_items_only() -> None:
    c = _db()
    at = (SWITCH + timedelta(minutes=5)).isoformat()
    c.execute("INSERT INTO event_items VALUES ('e1','live','high',?)", (at,))
    c.execute("INSERT INTO event_items VALUES ('e2','live','high',?)", (at,))
    c.execute("INSERT INTO event_item_members VALUES ('e1','x')")
    c.execute("INSERT INTO event_item_members VALUES ('e2','x'),('e2','y')")
    assert psc.window_counts(c, WIN)["solo_reports"] == 1


def test_judge_flags_outside_range_and_far_from_median() -> None:
    base = [10, 12, 11, 10, 13]
    assert psc.judge(11, base)[1] == "ok"
    assert psc.judge(0, base)[1] == "flag"
    assert psc.judge(30, base)[1] == "flag"
    assert psc.judge(5, [])[1] == "undetermined"


def test_run_check_flags_drop_against_baseline() -> None:
    c = _db()
    _fetch_runs(c, WIN, 3)
    for k in range(1, 15):
        past = WIN.shifted_days(k)
        _fetch_runs(c, past, 3)
        for n in range(10):
            _art(c, f"b{k}_{n}", "alert", "high", past.start + timedelta(minutes=n))
    report = psc.run_check(c, WIN)
    alert = next(m for m in report["metrics"] if m["name"] == "posted_alert")
    assert alert["status"] == "flag"
    assert report["verdict"] == "flag"
    assert report["baseline_days_used"] == 14


def test_low_coverage_is_undetermined() -> None:
    c = _db()
    _fetch_runs(c, WIN, 1)  # 3 時間中 1 時間 = 33%
    report = psc.run_check(c, WIN)
    assert report["verdict"] == "undetermined"
    assert all(m["status"] == "undetermined" for m in report["metrics"])


def test_no_bare_group_by() -> None:
    assert not any("GROUP BY" in sql.upper() for sql, _ in psc._METRICS.values())
