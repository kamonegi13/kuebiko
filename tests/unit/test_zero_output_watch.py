"""「成功なのに要約 0 件」の検知 (2026-11-18)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.storage.run_history import RunHistoryRepository, RunRecord
from src.ui.services.source_health import build_heartbeat_text
from src.ui.services.zero_output_watch import (
    RunTotals,
    build_zero_output_line,
    detect_zero_output,
)

NOW = datetime(2026, 11, 17, 23, 0, tzinfo=UTC)


def _run(
    pipeline: str, hours_ago: float, fetched: int, summarized: int, status: str = "succeeded"
) -> RunTotals:
    return RunTotals(pipeline, NOW - timedelta(hours=hours_ago), status, fetched, summarized)


def _baseline(pipeline: str) -> RunTotals:
    return _run(pipeline, 24 * 5, 6, 20)


def test_flags_pipeline_that_fetches_but_never_summarizes() -> None:
    runs = [_baseline("grok"), *(_run("grok", h, 6, 0) for h in (2, 12, 30))]
    hits = detect_zero_output(runs, now=NOW)
    assert [(h.pipeline, h.runs, h.fetched) for h in hits] == [("grok", 3, 18)]


def test_one_summarized_run_clears_the_flag() -> None:
    runs = [
        _baseline("grok"),
        _run("grok", 2, 6, 0),
        _run("grok", 12, 6, 1),
        _run("grok", 30, 6, 0),
    ]
    assert detect_zero_output(runs, now=NOW) == []


def test_needs_min_runs_with_fetches() -> None:
    # 取得 0 の run と失敗した run は数えない (取得なし・失敗は別の監視の担当)
    runs = [
        _baseline("grok"),
        _run("grok", 2, 6, 0),
        _run("grok", 12, 0, 0),
        _run("grok", 20, 6, 0, status="failed"),
        _run("grok", 30, 6, 0),
    ]
    assert detect_zero_output(runs, now=NOW) == []


def test_pipelines_that_never_summarize_are_not_flagged() -> None:
    runs = [_run("ransomware", h, 30, 0) for h in (2, 12, 30, 24 * 5)]
    assert detect_zero_output(runs, now=NOW) == []


def test_line_reads_runs_table(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "r.db")
    for hours_ago, summarized in ((24 * 5, 20), (2, 0), (12, 0), (30, 0)):
        run_id = repo.start_run(
            RunRecord(
                started_at=NOW - timedelta(hours=hours_ago), pipeline="grok-briefing", dry_run=False
            )
        )
        repo.finish_run(
            run_id,
            status="succeeded",
            finished_at=NOW - timedelta(hours=hours_ago) + timedelta(minutes=1),
            total_fetched=6,
            summarized=summarized,
            posted=0,
            marked_read=0,
            error_count=0,
        )
    line = build_zero_output_line(repo, now=NOW)
    assert line is not None and line.startswith("⚠️") and "grok-briefing" in line


def test_heartbeat_escalates_on_zero_output_line() -> None:
    _, body, importance = build_heartbeat_text(
        run_counts={"succeeded": 10},
        silent=[],
        feeds_total=1,
        zero_output_line="⚠️48h 取得ありで要約0件: grok-briefing(取得18件/3回)",
    )
    assert "grok-briefing" in body
    assert importance == "medium"
