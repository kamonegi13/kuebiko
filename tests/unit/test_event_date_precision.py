"""event_date_precision (日付の粒度): 判定・永続化・埋め直し規則。event_date の意味は不変。"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from src.pipeline.summary import _normalize_iso_date, _normalize_temporal, event_date_precision
from src.storage.event_date_precision_backfill import build_backfill_statements

_REF = date(2026, 6, 27)


@pytest.mark.parametrize(
    ("raw", "basis", "expected"),
    [
        ("2026-03", "occurred", "month"),
        ("2026-03-10", "disclosed", "day"),
        ("2026-03-01", "occurred", "unknown"),
        ("2026-03-20", "relative", "relative"),
        ("3ヶ月前", "occurred", None),
    ],
)
def test_precision_from_raw_string(raw: str, basis: str, expected: str | None) -> None:
    # Arrange
    normalized = _normalize_iso_date(raw, ceiling=_REF)
    # Act / Assert
    assert event_date_precision(raw, normalized, basis) == expected


def test_month_only_still_normalizes_to_first_day() -> None:
    # Arrange / Act
    ev, basis, _ = _normalize_temporal("2026-03", "occurred", None, reference=_REF)
    # Assert: 既存の読み手が見る event_date は従来どおり当月 1 日
    assert (ev, basis) == ("2026-03-01", "occurred")


def test_precision_none_without_event_date() -> None:
    assert event_date_precision(None, None, None) is None


def test_backfill_rules_fill_only_unrecorded_rows() -> None:
    # Arrange
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE articles (event_date TEXT, event_date_basis TEXT, event_date_precision TEXT)"
    )
    con.executemany(
        "INSERT INTO articles VALUES (?, ?, ?)",
        [
            ("2026-03-10", "occurred", None),  # day
            ("2026-03-01", "occurred", None),  # unknown
            ("2026-03-01", "relative", None),  # relative
            ("2026-03-05", "relative", None),  # relative (relative が優先)
            ("2026-03-01", "occurred", "month"),  # 記録済みは触らない
            (None, None, None),  # 日付なしは触らない
        ],
    )
    # Act
    for stmt in build_backfill_statements():
        con.execute(stmt)
    got = [r[0] for r in con.execute("SELECT event_date_precision FROM articles ORDER BY rowid")]
    # Assert
    assert got == ["day", "unknown", "relative", "relative", "month", None]
