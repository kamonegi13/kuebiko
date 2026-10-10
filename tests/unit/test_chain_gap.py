from datetime import UTC, datetime, timedelta

from src.ui.services.source_health import build_heartbeat_text, detect_chain_gaps

NOW = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)


def _hourly(hours: list[int]) -> list[datetime]:
    return [NOW - timedelta(hours=h) for h in hours]


def test_no_gap_when_hourly_runs_are_continuous() -> None:
    assert detect_chain_gaps(_hourly(list(range(0, 24))), now=NOW) == []


def test_reports_interior_gap_of_three_hours_or_more() -> None:
    hours = [h for h in range(0, 24) if h not in (8, 9, 10)]
    gaps = detect_chain_gaps(_hourly(hours), now=NOW)
    assert len(gaps) == 1
    assert gaps[0][1] - gaps[0][0] == timedelta(hours=4)


def test_reports_ongoing_gap_when_nothing_ran_recently() -> None:
    gaps = detect_chain_gaps(_hourly([10, 11, 12]), now=NOW)
    assert gaps and gaps[-1][1] == NOW


def test_heartbeat_marks_medium_when_gap_line_warns() -> None:
    _, body, importance = build_heartbeat_text(
        run_counts={"succeeded": 1},
        silent=[],
        feeds_total=1,
        chain_gap_line="⚠️毎時チェーン欠落(JST): x",
    )
    assert importance == "medium" and "欠落" in body


async def test_chain_records_skip_reason_in_detail() -> None:
    from src.scheduler.job_chain import ChainStep, run_chain

    records: list[tuple[str, str, str]] = []

    async def skipped() -> dict[str, str]:
        return {"skipped": "flag_off"}

    await run_chain(
        "c",
        [ChainStep("a", skipped, 5.0)],
        record=lambda j, s, d: records.append((j, s, d)),
    )
    assert records[0][1] == "succeeded" and "skipped=flag_off" in records[0][2]
