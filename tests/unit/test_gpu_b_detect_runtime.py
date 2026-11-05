"""GPU-B 道具の修正: 失敗の記録・実行時 read-only 検査・旗の突き合わせ・P3 の集計。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "gpu_window"))
sys.path.insert(0, str(_SCRIPTS))

import detect_arms as da  # noqa: E402
import detect_common as dc  # noqa: E402


def _stub_context(*_a: Any, **_k: Any) -> Any:
    return type("C", (), {"llm_input": [], "active_titles": []})()


def test_raising_arm_is_recorded_as_failure_not_empty_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.assessment.detect_step as step

    async def boom(_ctx: Any) -> Any:
        raise ConnectionError("llm down")

    monkeypatch.setattr(da, "build_context", _stub_context)
    monkeypatch.setattr(step, "decide_detect", boom)
    rec = asyncio.run(da.run_once(dc.SHIP_ARM, {"window_id": "w", "merge": None}, 1, object(), []))
    assert rec["ok"] is False and rec["error"] == "ConnectionError"
    assert "n_opened" not in rec


def test_timeout_is_recorded_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.assessment.detect_step as step

    async def slow(_ctx: Any) -> Any:
        await asyncio.sleep(5)

    monkeypatch.setattr(da, "build_context", _stub_context)
    monkeypatch.setattr(step, "decide_detect", slow)
    rec = asyncio.run(
        da.run_once(dc.CURRENT_ARM, {"window_id": "w", "merge": None}, 1, object(), [], 0.05)
    )
    assert rec["ok"] is False and rec["error"] == "TimeoutError"


def test_units_share_one_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """全単位が同じ event loop で走る (単位ごとの asyncio.run による loop 閉鎖事故の回避)。"""
    loops: list[int] = []

    async def fake_run_once(*_a: Any, **_k: Any) -> dict[str, Any]:
        loops.append(id(asyncio.get_running_loop()))
        return {"ok": True, "elapsed_s": 0.0}

    class Sink:
        def write(self, _s: str) -> None: ...
        def flush(self) -> None: ...

    monkeypatch.setattr(da, "run_once", fake_run_once)
    windows: dict[str, dict[str, Any]] = {"w1": {}, "w2": {}}
    units = dc.plan_units(["w1", "w2"], 1)
    n_ok, hit = asyncio.run(
        da.run_units(
            units, windows, dict.fromkeys(("current", "ship"), 0), [], Sink(), None, lambda _r: None
        )
    )
    assert (n_ok, hit) == (4, False) and len(set(loops)) == 1


def test_swallowed_failure_with_no_openings_is_failure() -> None:
    assert da.classify_failure({"n_opened": 0}, ["detect_decompose_failed"]).startswith(
        "swallowed:"
    )
    assert da.classify_failure({"n_opened": 2}, ["detect_decompose_failed"]) == ""
    assert da.classify_failure({"n_opened": 0}, []) == ""


def test_log_spy_collects_failure_events_only() -> None:
    spy = dc.LogSpy()
    spy.warning("detect_ml_union_failed", error="X")
    spy.warning("detect_decompose_shadow_timeout")
    spy.warning("something_else")
    spy.info("detect_decompose_applied")
    assert spy.failures() == ["detect_ml_union_failed", "detect_decompose_shadow_timeout"]


def test_unit_timeout_is_capped_by_deadline() -> None:
    assert dc.unit_timeout(1500.0, None) == 1500.0
    assert dc.unit_timeout(1500.0, 60.0) == 60.0
    assert dc.unit_timeout(30.0, 600.0) == 30.0


class _Conn:
    def __init__(self, value: str) -> None:
        self._v = value

    def execute(self, _sql: str) -> _Conn:
        return self

    def fetchone(self) -> dict[str, str]:
        return {"default_transaction_read_only": self._v}

    def close(self) -> None:
        return None


def test_runtime_read_only_check_requires_both_connections() -> None:
    ok = dc.verify_read_only_runtime(lambda: _Conn("on"), lambda: _Conn("on"))
    assert ok == {"raw_connect": "on", "patched_connect": "on"}
    with pytest.raises(dc.WriteGuardError):
        dc.verify_read_only_runtime(lambda: _Conn("off"), lambda: _Conn("on"))
    with pytest.raises(dc.WriteGuardError):
        dc.verify_read_only_runtime(lambda: _Conn("on"), lambda: _Conn("off"))


def test_prefer_read_only_role_swaps_database_url() -> None:
    env = {"DATABASE_URL": "owner", dc.READ_ONLY_URL_ENV: "ro"}
    assert dc.prefer_read_only_role(env) == "dedicated_role" and env["DATABASE_URL"] == "ro"
    env2 = {"DATABASE_URL": "owner"}
    assert dc.prefer_read_only_role(env2) == "owner_role_with_read_only_session"
    assert env2["DATABASE_URL"] == "owner"


def test_flag_mismatch_detects_assign_gate_difference_and_exempts_arm_flags() -> None:
    prod = {"ASSIGN_EMBED_GATE": "on", "DETECT_DECOMPOSE": "shadow", "EVENTNEWS_X": "1"}
    same = dict(prod, DETECT_DECOMPOSE="1")  # 腕ごとに固定する旗は免除
    assert dc.flag_mismatches(prod, same) == []
    diff = dc.flag_mismatches(prod, dict(same, ASSIGN_EMBED_GATE="shadow"))
    assert len(diff) == 1 and diff[0].startswith("ASSIGN_EMBED_GATE")
    assert dc.runtime_flags({"EVENTNEWS_A": "1", "OTHER": "x"})["EVENTNEWS_A"] == "1"


def test_p3_gate_share_uses_blocked_over_opened_plus_blocked() -> None:
    recs = [
        {"arm": "ship", "ok": True, "fallback": False, "n_opened": 20, "n_gate_blocked": 8},
        {"arm": "ship", "ok": True, "fallback": True, "n_opened": 9, "n_gate_blocked": 9},
        {"arm": "current", "ok": True, "n_opened": 50, "n_gate_blocked": 0},
        {"arm": "ship", "ok": False, "n_opened": 5, "n_gate_blocked": 5},
    ]
    p3 = dc.p3_gate_share(recs)
    assert p3["blocked"] == 8 and p3["opened"] == 20 and p3["denominator"] == 28
    assert p3["share"] == pytest.approx(8 / 28) and p3["undetermined"] is True
    big = [{"arm": "ship", "ok": True, "fallback": False, "n_opened": 40, "n_gate_blocked": 10}]
    assert dc.p3_gate_share(big)["undetermined"] is False
