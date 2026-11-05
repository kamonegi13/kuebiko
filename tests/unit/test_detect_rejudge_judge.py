"""DETECT-REJUDGE の裁定・採点の道具 (scripts/gpu_window/detect_judge*.py の rejudge プロファイル)。

Opus は呼ばない。合成した 2 腕の出力 (fixture) で plan → 偽の裁定 → score が通ること、
前回 (decompose) の既定が変わっていないことを確かめる。
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "gpu_window"))
import detect_judge_core as jc_default  # noqa: E402

N_WINDOWS = 20
RUNS = 3


def _load(profile: str | None, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, ModuleType]:
    if profile is None:
        monkeypatch.delenv("DETECT_GPU_PROFILE", raising=False)
    else:
        monkeypatch.setenv("DETECT_GPU_PROFILE", profile)
    jc = importlib.reload(importlib.import_module("detect_judge_core"))
    dj = importlib.reload(importlib.import_module("detect_judge"))
    return jc, dj


@pytest.fixture
def rejudge(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[ModuleType, ModuleType]]:
    yield _load("rejudge", monkeypatch)
    monkeypatch.undo()
    _load(None, monkeypatch)


def _rec(window: str, arm: str, run: int, claims: list[tuple[list[str], str]]) -> dict[str, Any]:
    return {
        "window_id": window,
        "arm": arm,
        "run": run,
        "opened": [{"article_ids": ids, "claim": c} for ids, c in claims],
        "n_opened": len(claims),
        "fallback": False,
        "variant": "decompose" if arm == "ship" else "current",
        "n_gate_blocked": 0,
        "n_fidelity_blocked": 2 if arm == "ship" else 0,
        "n_one_off_skipped": 1 if arm == "ship" else 0,
    }


def _build_eval_dir(tmp: Path) -> list[str]:
    """20 窓。各窓: 共有 1 件 + 現行だけ 1 件 + 出荷だけ 1 件。w00 の共有は汚染記事を含む。"""
    records: list[dict[str, Any]] = []
    universe: list[str] = []
    for i in range(N_WINDOWS):
        w = f"w{i:02d}"
        shared = ([f"{w}:a"], f"{w} shared kuebiko.example")
        cur = ([f"{w}:b"], f"{w} current kuebiko.example")
        shp = ([f"{w}:c"], f"{w} ship kuebiko.example")
        universe += [f"{w}:a", f"{w}:b", f"{w}:c"]
        for run in range(RUNS):
            records.append(_rec(w, "current", run, [shared, cur]))
            records.append(_rec(w, "ship", run, [shared, shp]))
    (tmp / "arms_20261110T000000Z.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    (tmp / "inputs.json").write_text(json.dumps({"windows": []}), encoding="utf-8")
    (tmp / "closure_plan.json").write_text(
        json.dumps(
            {
                "chosen_step": "L1",
                "r18_version": "R18.2",
                "contaminated_articles": ["w00:a"],
                "frozen_universe": universe,
            }
        ),
        encoding="utf-8",
    )
    return universe


def _point(dj: ModuleType, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dj, "EVAL_DIR", tmp)
    monkeypatch.setattr(dj, "JUDGE_DIR", tmp / "judge")
    monkeypatch.setattr(dj, "CLOSURE_PATH", tmp / "closure_plan.json")
    monkeypatch.setattr(dj, "ARMS_OVERRIDE", {})


def _fake_verdicts(dj: ModuleType, plan: dict[str, Any], tmp: Path) -> None:
    """全単位 yes、5 件おきに q1=no (偽の裁定)。"""
    rows = {}
    for uid in plan["sampled"]:
        rows[uid] = dj.DetectVerdict(q1_new_event="yes", q2_trackable="yes", q3_faithful="yes")
    for uid in plan["sampled"][::5]:
        rows[uid] = dj.DetectVerdict(q1_new_event="no", q2_trackable="yes", q3_faithful="yes")
    dj.write_verdicts(tmp / "judge" / "verdicts.jsonl", rows)
    second = {u: rows[u] for u in plan["second"]}
    dj.write_verdicts(tmp / "judge" / "verdicts_pass2.jsonl", second)


def test_rejudge_profile_constants(rejudge: tuple[ModuleType, ModuleType]) -> None:
    jc, dj = rejudge
    assert jc.SEED == 20261105
    assert dj.IS_REJUDGE
    assert dj.JUDGE_DIR.parts[-4:] == ("data", "eval", "detect_rejudge", "judge")
    assert dj.RUBRIC != "detect-judge-v1" and dj.RUBRIC_P2 != dj.RUBRIC
    assert dj.CALL_CAP == 390
    assert dj.GOLD_ID == "gold:detect_rejudge_past"
    assert dj.CLOSURE_PATH.name == "closure_plan.json"


def test_default_profile_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    jc, dj = _load(None, monkeypatch)
    assert jc.SEED == 20261104
    assert not dj.IS_REJUDGE
    assert dj.JUDGE_DIR.name == "judge_r18"
    assert dj.ARMS_NAME == "arms_20261104T022916Z.jsonl"
    assert dj.ITEM == "DETECT-JUDGE-R18"
    assert dj.RUBRIC == "detect-judge-v1"
    assert dj.CLOSURE_PATH.name == "closure.json"


def test_unknown_profile_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DETECT_GPU_PROFILE", "nope")
    with pytest.raises(SystemExit):
        importlib.reload(importlib.import_module("detect_judge_core"))
    _load(None, monkeypatch)


def test_decide_flags_excluded_share_undetermined() -> None:
    jc = jc_default
    stats = {
        "n_windows": 20,
        "mean_d_ci": (-0.2, 0.5),
        "wilcoxon_p": 0.5,
        "median_d": 0.1,
        "false_rate_diff_ci": (-0.1, 0.02),
    }
    p3 = {"denominator": 100, "share": 0.1}
    ok = jc.decide(stats, p3, {"share": 1.0}, {"ratio": 1.0}, 0.9, excluded_share=0.5)
    assert ok["verdict"] == "pass"
    bad = jc.decide(stats, p3, {"share": 1.0}, {"ratio": 1.0}, 0.9, excluded_share=0.51)
    assert bad["verdict"] == "undetermined"
    assert jc.decide(stats, p3, {"share": 1.0}, {"ratio": 1.0}, 0.9)["verdict"] == "pass"


def test_p3_uses_gate_only_and_reports_fidelity_separately() -> None:
    jc = jc_default
    recs = [
        {"arm": "ship", "n_gate_blocked": 1, "n_opened": 3, "n_fidelity_blocked": 5},
        {"arm": "ship", "n_gate_blocked": 0, "n_opened": 1, "n_one_off_skipped": 2},
    ]
    p3 = jc.p3_share(recs)
    assert p3["blocked"] == 1 and p3["denominator"] == 5 and p3["share"] == 0.2
    assert p3["fidelity_blocked"] == 5 and p3["one_off_skipped"] == 2


def test_closure_plan_excludes_contaminated_and_outside_universe() -> None:
    jc = jc_default
    recs = [
        {
            "window_id": "w",
            "arm": "ship",
            "opened": [
                {"article_ids": ["a"], "claim": "x"},
                {"article_ids": ["b"], "claim": "y"},
                {"article_ids": ["z"], "claim": "q"},
                {"article_ids": ["c", "a"], "claim": "m"},
            ],
        }
    ]
    units = jc.collect_units(recs)
    out = jc.excluded_by_closure_plan(units, frozenset({"a"}), frozenset({"a", "b", "c"}))
    claims = {units[u].claim for u in out}
    assert claims == {"x", "q", "m"}


def test_plan_adjudicate_score_end_to_end(
    rejudge: tuple[ModuleType, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jc, dj = rejudge
    _build_eval_dir(tmp_path)
    _point(dj, tmp_path, monkeypatch)

    assert dj.cmd_plan(argparse.Namespace()) == 0
    plan = json.loads((tmp_path / "judge" / "plan.json").read_text(encoding="utf-8"))
    assert plan["seed"] == 20261105
    assert plan["arms_name"] == "arms_20261110T000000Z.jsonl"
    # 汚染記事 w00:a を持つ共有の単位だけが外れる
    assert plan["n_r18_excluded"] == 1
    assert plan["n_units_total"] == 3 * N_WINDOWS
    assert len(plan["sampled"]) == 3 * N_WINDOWS - 1  # U ≤ 260 は全数
    assert len(plan["second"]) == 40

    _fake_verdicts(dj, plan, tmp_path)
    assert dj.cmd_score(argparse.Namespace()) == 0
    summary = json.loads((tmp_path / "judge" / "summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "judge" / "manifest.json").read_text(encoding="utf-8"))
    assert summary["decision"]["verdict"] in {"pass", "fail"}
    assert summary["stats"]["n_windows"] == N_WINDOWS
    assert summary["p4"]["share"] == 1.0
    assert summary["p3"]["fidelity_blocked"] == 2 * N_WINDOWS * RUNS
    assert summary["determinable_share_of_sampled"] == 1.0
    assert manifest["gold_id"] == "gold:detect_rejudge_past"
    assert manifest["prereg_path"].endswith("detect_decompose_rejudge.md")
    assert manifest["seed"] == 20261105


def test_plan_refuses_previous_judge_dir(
    rejudge: tuple[ModuleType, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dj = rejudge
    _build_eval_dir(tmp_path)
    _point(dj, tmp_path, monkeypatch)
    monkeypatch.setattr(dj, "FORBIDDEN_JUDGE_DIRS", (tmp_path / "judge",))
    with pytest.raises(SystemExit):
        dj.cmd_plan(argparse.Namespace())


def test_excluded_over_half_is_undetermined(
    rejudge: tuple[ModuleType, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dj = rejudge
    universe = _build_eval_dir(tmp_path)
    plan_path = tmp_path / "closure_plan.json"
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    data["contaminated_articles"] = [a for a in universe if not a.endswith(":c")]
    plan_path.write_text(json.dumps(data), encoding="utf-8")
    _point(dj, tmp_path, monkeypatch)
    dj.cmd_plan(argparse.Namespace())
    plan = json.loads((tmp_path / "judge" / "plan.json").read_text(encoding="utf-8"))
    _fake_verdicts(dj, plan, tmp_path)
    dj.cmd_score(argparse.Namespace())
    summary = json.loads((tmp_path / "judge" / "summary.json").read_text(encoding="utf-8"))
    assert summary["decision"]["verdict"] == "undetermined"
    assert any("50%" in r for r in summary["decision"]["reasons"])


def test_missing_chosen_step_stops(
    rejudge: tuple[ModuleType, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dj = rejudge
    _build_eval_dir(tmp_path)
    plan_path = tmp_path / "closure_plan.json"
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    data["chosen_step"] = None
    plan_path.write_text(json.dumps(data), encoding="utf-8")
    _point(dj, tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        dj.cmd_plan(argparse.Namespace())
