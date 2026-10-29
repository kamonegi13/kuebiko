"""週次監査の「alert の取込遅延 p90」の節 (計画 AT-7)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.storage.run_history import RunHistoryRepository
from src.ui.services.fill_rate_alert_latency_section import (
    alert_latency_line,
    collect_alert_latency,
    percentile,
    summarize_latency,
)

_UNTIL = datetime(2026, 10, 26, 0, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_percentile_interpolates_linearly() -> None:
    assert percentile([0.0, 10.0], 0.9) == 9.0
    assert percentile([], 0.9) is None


def test_summarize_excludes_negative_and_missing_published() -> None:
    base = _UNTIL
    pairs = [
        (_iso(base), _iso(base - timedelta(hours=2))),
        (_iso(base), _iso(base + timedelta(hours=1))),  # 負
        (_iso(base), None),
    ]

    stats = summarize_latency(pairs)

    assert (stats.n_total, stats.n_measured, stats.n_negative, stats.n_no_published) == (3, 1, 1, 1)
    assert stats.p90_hours == 2.0


def test_line_without_measurements_does_not_crash() -> None:
    assert "0" in alert_latency_line(summarize_latency([]))


def test_line_reports_p90() -> None:
    line = alert_latency_line(
        summarize_latency([(_iso(_UNTIL), _iso(_UNTIL - timedelta(hours=3)))])
    )

    assert "p90 3.0h" in line and "n=1/1" in line


def _seed(
    repo: RunHistoryRepository, aid: str, channel: str, created: datetime, pub: datetime | None
) -> None:
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (started_at, pipeline, dry_run, status) VALUES (?, 't', 0, 'done')",
            (_iso(created),),
        )
        rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (run_id, article_id, title, url, status, created_at,"
            " posted_channel, published_at) VALUES (?,?,?,?, 'posted', ?, ?, ?)",
            (
                rid,
                aid,
                aid,
                f"https://kuebiko.example/{aid}",
                _iso(created),
                channel,
                _iso(pub) if pub else None,
            ),
        )


def test_collect_windows_by_event_time_not_ingest_time(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "lat.db")
    in_window = _UNTIL - timedelta(days=2)
    # 窓内に公開 → 計上 (遅延 4h)
    _seed(repo, "in", "alert", in_window + timedelta(hours=4), in_window)
    # 窓の外に公開されたがバックフィルで窓内に取込 → 除外
    _seed(repo, "backfill", "alert", in_window, in_window - timedelta(days=30))
    # alert 以外 → 除外
    _seed(repo, "other", "watch", in_window + timedelta(hours=1), in_window)

    stats = collect_alert_latency(repo, until=_UNTIL)

    assert stats.n_total == 1
    assert stats.p90_hours == 4.0
