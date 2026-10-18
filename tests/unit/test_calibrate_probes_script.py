"""scripts/calibrate_probes.py のテスト (GPU なしの部分: fit / gate / 経路記録 / 準備)。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "calibrate_probes.py"


@pytest.fixture(name="cp")
def _cp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("calibrate_probes_under_test", _PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(mod, "CALIB", tmp_path / "calib")
    (tmp_path / "runs").mkdir()
    (tmp_path / "calib").mkdir()
    return mod


def _thin_rows() -> list[dict[str, Any]]:
    rows = [{"id": f"t{i}", "group": f"t{i}", "label": 0, "weight": 1.0} for i in range(30)]
    rows += [{"id": f"c{i}", "group": f"c{i}", "label": 1, "weight": 1.0} for i in range(60)]
    return rows


def _write_runs(cp: ModuleType, model: str, scores: dict[str, float]) -> None:
    path = cp.RUNS / f"{cp.safe_name(model)}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for id_, p in scores.items():
            fh.write(
                json.dumps({"model": model, "task": "thin", "id": id_, "p": p, "both_rate": 1.0})
                + "\n"
            )


def test_safe_name(cp: ModuleType) -> None:
    assert cp.safe_name("kuebiko-sft:s24") == "kuebiko-sft_s24"


def test_fit_thin_picks_threshold_and_oof(cp: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(cp.LOADERS, "thin", _thin_rows)
    scores = {f"t{i}": 0.01 + i * 0.002 for i in range(30)} | {
        f"c{i}": 0.5 + i * 0.005 for i in range(60)
    }
    _write_runs(cp, "m:a", scores)

    result = cp.fit_single("thin", cp.joined("thin", "m:a"))

    assert result["auc"] == 1.0
    assert 0.07 < result["threshold"]["main"] <= 0.51
    assert result["out_of_fold"]["recall"] == 1.0
    assert result["both_choices_rate"] == 1.0


def test_fit_writes_only_the_target_model(
    cp: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(cp.LOADERS, "thin", _thin_rows)
    for task in ("claim", "assign", "org"):
        monkeypatch.setitem(cp.LOADERS, task, lambda: [])
    scores = {f"t{i}": 0.01 + i * 0.002 for i in range(30)} | {
        f"c{i}": 0.5 + i * 0.005 for i in range(60)
    }
    _write_runs(cp, "m:a", scores)
    table = tmp_path / "probe_thresholds.json"
    table.write_text(
        json.dumps({"old:model": {"thin_body_judge": {"main": 0.075}}}), encoding="utf-8"
    )
    monkeypatch.setattr(cp, "THRESHOLDS_PATH", table)
    monkeypatch.setattr(
        cp, "load_thresholds", lambda: json.loads(table.read_text(encoding="utf-8"))
    )
    monkeypatch.setattr(
        cp, "save_thresholds", lambda t: table.write_text(json.dumps(t), encoding="utf-8")
    )

    assert cp.cmd_fit("m:a", write=True) == 0

    saved = json.loads(table.read_text(encoding="utf-8"))
    assert saved["old:model"] == {"thin_body_judge": {"main": 0.075}}  # 戻し先の項は残る
    assert "main" in saved["m:a"]["thin_body_judge"]
    assert "pir_focus_claim_judge" not in saved["m:a"]  # runs の無い判定は書かない


def test_fit_without_write_does_not_touch_the_table(
    cp: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(cp.LOADERS, "thin", _thin_rows)
    for task in ("claim", "assign", "org"):
        monkeypatch.setitem(cp.LOADERS, task, lambda: [])
    _write_runs(cp, "m:a", {f"t{i}": 0.1 for i in range(30)} | {f"c{i}": 0.9 for i in range(60)})
    called: list[Any] = []
    monkeypatch.setattr(cp, "save_thresholds", lambda t: called.append(t))

    cp.cmd_fit("m:a", write=False)

    assert called == []


def test_gate_step_checks(cp: ModuleType) -> None:
    base = {"auc": 0.98, "confident_band_accuracy": 0.95,
            "at_current_constant": {"recall": 0.6, "precision": 0.9}}  # fmt: skip
    good = {"auc": 0.97, "confident_band_accuracy": 0.96, "both_choices_rate": 0.99,
            "out_of_fold": {"recall": 0.7, "precision": 0.92}}  # fmt: skip
    bad = {**good, "auc": 0.90, "both_choices_rate": 0.5}

    ok = cp.gate_step(good, base, "thin_body_judge")
    ng = cp.gate_step(bad, base, "thin_body_judge")

    assert all(v is True for v in ok.values())
    assert ng["auc"] is False
    assert ng["both_choices"] is False


def test_gate_org_requires_zero_false_same(cp: ModuleType) -> None:
    base = {"auc_margin": 0.95}
    cand = {
        "auc_margin": 0.95,
        "out_of_fold": {"recall": 0.9, "false_fire": 0.0},
        "both_choices_rate": 1.0,
    }
    assert cp.gate_step(cand, base, "org_identity_judge")["precision"] is True
    cand_bad = {**cand, "out_of_fold": {"recall": 0.9, "false_fire": 0.01}}
    assert cp.gate_step(cand_bad, base, "org_identity_judge")["precision"] is False


def test_both_choices_recorder(cp: ModuleType) -> None:
    rec = cp.BothChoicesRecorder()
    wrapped = rec.wrap(lambda candidates, choices: {"Yes": 0.9, "No": 0.1})
    wrapped([("Yes", -0.1), ("No", -2.0)], ("Yes", "No"))
    wrapped([("Yes", -0.1), ("x", -2.0)], ("Yes", "No"))
    assert rec.drain() == 0.5
    assert rec.drain() is None


def test_prepare_model_adds_temp_tag_and_rejects_forbidden(
    cp: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SFT_TASK_PREFIX_MODELS", raising=False)
    cp.prepare_model("kuebiko-sft:s24x")
    from src.tools.decision_probe import _is_json_biased

    assert _is_json_biased("kuebiko-sft:s24x") is True
    monkeypatch.delenv("SFT_TASK_PREFIX_MODELS", raising=False)
    cp.prepare_model("gemma4:26b")
    assert _is_json_biased("gemma4:26b") is False
    with pytest.raises(Exception, match="qwen|forbidden|禁止"):
        cp.prepare_model("qwen3:7b")
