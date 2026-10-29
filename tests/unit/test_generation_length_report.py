"""scripts/generation_length_report.py: 百分位・JSON 欄の取り出し・集計・hit_token_cap の有無。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import generation_length_report as glr  # noqa: E402

SINCE = "2026-10-01T00:00:00+00:00"
AT = "2026-10-20T00:00:00+00:00"


def test_percentile_nearest_rank() -> None:
    vals = list(range(1, 11))
    assert glr.percentile(vals, 0.5) == 5
    assert glr.percentile(vals, 0.9) == 9
    assert glr.percentile([], 0.9) == 0
    assert glr.stats([3]) == {"n": 1, "p50": 3, "p90": 3, "max": 3}


def test_sir_summary_lengths_reads_pir_list_and_ignores_bad_payloads() -> None:
    good = json.dumps({"pir": [{"summary": "あいう"}, {"summary": 1}, {"title": "x"}]})
    assert glr.sir_summary_lengths([good, "{broken", None, json.dumps({"pir": "x"})]) == [3]


def test_event_body_lengths_extracts_bluf_and_key_points() -> None:
    body = json.dumps({"bluf": "abcd", "key_points": ["ab", "cde", 5]})
    assert glr.event_body_lengths([body, "oops"]) == ([4], [2, 3])


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE pir_spotlight(headline TEXT, outlook TEXT, generated_at TEXT);
        CREATE TABLE status_synthesis(headline TEXT, weight_section TEXT, chain_section TEXT,
          cog_section TEXT, spillover_section TEXT, pir_section TEXT, generated_at TEXT);
        CREATE TABLE weekly_recaps(recap_text TEXT, generated_at TEXT);
        CREATE TABLE event_item_versions(headline TEXT, body_json TEXT, generated_at TEXT);
        CREATE TABLE detect_bundles(claim TEXT, run_at TEXT);
        CREATE TABLE daily_briefs(payload TEXT, generated_at TEXT);
        CREATE TABLE llm_usage(ts TEXT);
        """
    )
    return c


def test_collect_counts_only_rows_inside_window() -> None:
    c = _db()
    c.execute("INSERT INTO pir_spotlight VALUES ('12345','abcdefghij',?)", (AT,))
    c.execute("INSERT INTO pir_spotlight VALUES ('old','old','2026-01-01T00:00:00+00:00')")
    c.execute("INSERT INTO detect_bundles VALUES ('claim', ?)", (AT,))
    c.execute(
        "INSERT INTO daily_briefs VALUES (?, ?)", (json.dumps({"pir": [{"summary": "xy"}]}), AT)
    )
    out = glr.collect(c, SINCE)
    assert out["spotlight_headline"] == [5]
    assert out["spotlight_outlook"] == [10]
    assert out["detect_claim"] == [5]
    assert out["sir_focus_summary"] == [2]


def test_token_cap_rate_reports_unavailable_without_column() -> None:
    c = _db()
    cap = glr.token_cap_rate(c, SINCE)
    assert cap["available"] is False


def test_token_cap_rate_computes_rate_when_column_exists() -> None:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE llm_usage(ts TEXT, hit_token_cap INTEGER)")
    c.execute("INSERT INTO llm_usage VALUES (?,1),(?,0),(?,0),(?,0)", (AT, AT, AT, AT))
    assert glr.token_cap_rate(c, SINCE) == {"available": True, "calls": 4, "hit": 1, "rate": 0.25}
