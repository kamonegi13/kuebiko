"""scripts/gpu_window/ の純粋部分 (計画・終了コード判定・ファイル書き出し)。"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "gpu_window"))
sys.path.insert(0, str(_SCRIPTS))

import build_pair_input as bpi  # noqa: E402
import day0_triage as d0  # noqa: E402
import gpu_window_common as common  # noqa: E402
import pair_same_input_control as psic  # noqa: E402
import pstop  # noqa: E402
import verify_sustained as vs  # noqa: E402


def _job(job_id: str, enabled: bool = True, **kw: Any) -> dict[str, Any]:
    return {"id": job_id, "enabled": enabled, **kw}


def test_plan_stops_only_enabled_non_chain_jobs() -> None:
    jobs = [
        _job("hourly-collect", protection="critical"),
        _job("hourly-upkeep", enabled=False),
        _job("grok-briefing", chain_id="hourly-collect"),
        _job("job-recovery-watchdog"),
    ]
    plan = {p.job_id: p for p in pstop.build_plan(jobs)}
    assert plan["hourly-collect"].action == "stop" and plan["hourly-collect"].critical
    assert plan["hourly-upkeep"].action == "skip"
    assert plan["grok-briefing"].action == "skip"
    assert plan["ransomware-live-ingest"].action == "skip"
    assert plan["job-recovery-watchdog"].action == "stop"


def test_stop_confirms_critical_and_restore_is_reverse_of_stopped_only() -> None:
    calls: list[tuple[str, bool, bool]] = []
    stopped: list[str] = []
    plan = pstop.build_plan([_job("hourly-collect", protection="critical"), _job("hourly-upkeep")])
    marks: list[int] = []
    pstop._stop_all(plan, lambda j, e, c: calls.append((j, e, c)), stopped, lambda: marks.append(1))
    assert calls == [("hourly-collect", False, True), ("hourly-upkeep", False, False)]
    assert len(marks) == 2
    calls.clear()
    assert pstop.restore_all(stopped, lambda j, e, c: calls.append((j, e, c))) == []
    assert calls == [("hourly-upkeep", True, False), ("hourly-collect", True, False)]


def test_failed_stop_midway_keeps_attempted_jobs_for_restore() -> None:
    stopped: list[str] = []

    def toggle(job_id: str, enabled: bool, confirm: bool) -> None:
        if job_id == "hourly-upkeep":
            raise OSError("boom")

    plan = pstop.build_plan([_job("hourly-collect", protection="critical"), _job("hourly-upkeep")])
    with pytest.raises(OSError):
        pstop._stop_all(plan, toggle, stopped, lambda: None)
    # 試みたものは (サーバ側で適用済みかもしれないので) 戻しの対象に残す
    assert stopped == ["hourly-collect", "hourly-upkeep"]


def test_restore_failure_reports_ids_and_manual_commands() -> None:
    def toggle(job_id: str, enabled: bool, confirm: bool) -> None:
        raise OSError("down")

    failed = pstop.restore_all(["a", "b"], toggle, sleep=lambda _: None, retries=2)
    assert failed == ["b", "a"]
    cmds = pstop.manual_restore_commands(failed, "http://x")
    assert "jobs/b/toggle" in cmds[0] and '"enabled": true' in cmds[0]


def test_marker_roundtrip(tmp_path: Path) -> None:
    m = tmp_path / "m.json"
    pstop.write_marker(m, ["a", "b"], datetime(2026, 11, 8, 15, tzinfo=common.JST))
    assert pstop.read_marker(m) == ["a", "b"]


def test_next_clock_rolls_to_tomorrow() -> None:
    now = datetime(2026, 11, 8, 16, 0, tzinfo=common.JST)
    assert common.next_clock(now, "15:00").day == 9
    assert common.next_clock(now, "17:00").day == 8


def test_busy_jobs() -> None:
    jobs: list[dict[str, Any]] = [
        {"id": "a", "last_run": {"status": "running"}},
        {"id": "b", "running_since": None},
    ]
    assert common.api_busy_jobs(jobs) == ["a"]


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({}, vs.EXIT_OK),
        ({"on_ac": False}, vs.EXIT_POWER),
        ({"lid_is_closed": True}, vs.EXIT_POWER),
        ({"tok_s": [80.0, 44.9]}, vs.EXIT_SLOW),
        ({"battery_drop": 1}, vs.EXIT_BATTERY),
    ],
)
def test_decide_exit_codes(kw: dict[str, Any], code: int) -> None:
    base: dict[str, Any] = {
        "on_ac": True,
        "lid_is_closed": False,
        "tok_s": [70.0, 80.0],
        "battery_drop": 0,
    }
    assert vs.decide(**{**base, **kw})[0] == code


def test_parse_pmset_and_lid() -> None:
    ac = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t80%; AC attached; not charging"
    p = vs.parse_pmset(ac)
    assert p.on_ac and p.battery_pct == 80
    assert not vs.parse_pmset("Now drawing from 'Battery Power'\n\t79%; discharging").on_ac
    assert vs.lid_closed('"AppleClamshellState" = Yes')
    assert not vs.lid_closed('"AppleClamshellState" = No')


def test_append_record_writes_json_line_even_on_failure(tmp_path: Path) -> None:
    out = tmp_path / "e.jsonl"
    rec = vs.build_record(
        ts="t",
        model="m",
        duration_s=1,
        tok_s=[],
        power="p",
        battery_start=80,
        battery_end=80,
        thermal_events=0,
        production="busy",
        code=vs.EXIT_BUSY,
        reason="r",
    )
    vs.append_record(out, rec)
    vs.append_record(out, rec)
    lines = out.read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["pass"] is False


def test_day0_refuses_same_path_and_keeps_now_26b(tmp_path: Path) -> None:
    src = tmp_path / "g.json"
    with pytest.raises(ValueError):
        d0.check_paths(src, src)
    gold: dict[str, Any] = {
        "meta": {"k": 1},
        "rows": [{"article_id": "a", "now_26b": "high"}, {"article_id": "b"}],
    }
    out = d0.build_output(gold, {"a": "medium"}, model="m", input_sha256="x")
    assert out["rows"][0] == {"article_id": "a", "now_26b": "high", "now_s": "medium"}
    assert "now_s" not in out["rows"][1] and gold["rows"][0].get("now_s") is None
    assert out["meta"]["now_s_judged"] == 1
    src.write_text("{}")
    hashes = d0.write_with_hashes(tmp_path / "o.json", out, "x")
    assert hashes["output_sha256"] == common.sha256_file(tmp_path / "o.json")


def test_pair_input_selection_by_period_and_direction() -> None:
    f3: list[dict[str, Any]] = [
        {"id": "a||b", "a": "a", "b": "b", "source": "pair_shadow", "stratum": "s", "weight": 2.0},
        {"id": "c||d", "a": "c", "b": "d", "source": "pair_shadow"},
        {"id": "e||f", "a": "e", "b": "f", "source": "other"},
    ]
    cands = bpi.load_f3_pairs(f3)
    assert [c["f3_id"] for c in cands] == ["a||b", "c||d"]
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE event_pair_shadow"
        "(observed_at TEXT, left_id TEXT, right_id TEXT, llm_same INTEGER)"
    )
    conn.execute("INSERT INTO event_pair_shadow VALUES ('2026-10-12T00:00:00+00:00','b','a',1)")
    conn.execute("INSERT INTO event_pair_shadow VALUES ('2026-10-30T00:00:00+00:00','c','d',1)")
    picked = bpi.select_pairs(cands, conn, bpi.S23I_START, bpi.S23I_END)
    assert [p["f3_id"] for p in picked] == ["a||b"]


def test_per_pair_records_have_ids_and_verdicts_only(tmp_path: Path) -> None:
    pairs = [psic.PairInput("a", "b")]
    rec = psic.per_pair_records(
        ["m1", "m2"], pairs, {"m1": [True], "m2": [None]}, {"m1": [False], "m2": [None]}
    )
    assert rec == [
        {
            "left_id": "a",
            "right_id": "b",
            "llm_same": {"m1": True, "m2": None},
            "ml_joined": {"m1": False, "m2": None},
        }
    ]
    out = tmp_path / "p.jsonl"
    psic.write_per_pair(out, rec)
    assert json.loads(out.read_text().strip())["left_id"] == "a"


def test_intent_is_marked_before_toggle() -> None:
    seen: list[list[str]] = []
    stopped: list[str] = []
    plan = pstop.build_plan([_job("hourly-upkeep")])
    pstop._stop_all(
        plan,
        lambda j, e, c: seen.append(list(stopped)),
        stopped,
        lambda: None,
    )
    assert seen == [["hourly-upkeep"]]


def test_verify_stopped_and_validate_times() -> None:
    assert pstop.verify_stopped([_job("a"), _job("b", enabled=False)], ["a", "b"]) == ["a"]
    j = common.JST
    now = datetime(2026, 11, 8, 10, 0, tzinfo=j)
    ok = pstop.validate_times(
        now, datetime(2026, 11, 8, 12, tzinfo=j), datetime(2026, 11, 8, 15, tzinfo=j)
    )
    assert ok is None
    assert pstop.validate_times(
        now, datetime(2026, 11, 8, 12, tzinfo=j), datetime(2026, 11, 9, 1, tzinfo=j)
    )
    assert pstop.validate_times(
        now, datetime(2026, 11, 8, 15, tzinfo=j), datetime(2026, 11, 8, 12, tzinfo=j)
    )


def test_thermal_parsing_fail_closed_and_ignores_battery() -> None:
    log = (
        "2026-10-30 11:00:00 +0900 Thermal Emergency\n"
        "2026-10-30 11:01:00 +0900 Thermal Emergency Using Batt\n"
    )
    assert vs.count_emergency(log) == 1
    with pytest.raises(vs.ThermalParseError):
        vs.count_emergency("garbage")
    assert vs.cpu_limited("CPU_Speed_Limit = 80") and not vs.cpu_limited("CPU_Speed_Limit = 100")


def test_battery_noise_only_on_ac_throughout() -> None:
    ac = vs.Power(True, 99, "AC attached; charging")
    batt = vs.Power(False, 99, "discharging")
    assert vs.battery_drop_is_noise(ac, ac)
    assert not vs.battery_drop_is_noise(ac, batt)
    assert not vs.battery_drop_is_noise(
        vs.Power(True, 80, "not charging"), vs.Power(True, 78, "not charging")
    )
