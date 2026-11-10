"""S 切替の関門と学習出力の解析 (純粋関数)。"""

from __future__ import annotations

from typing import Any

from src.tools.ml_s_dependency import RETRAIN_TARGETS, derive_dependencies
from src.tools.s_switch_gate import (
    FAIL,
    PASS,
    UNMEASURED,
    evaluate_gates,
    gate_detect,
    gate_pair,
    gate_severity_s3,
    parse_deep_dive_stdout,
    parse_relation_stdout,
    parse_severity_stdout,
)

GOOD: dict[str, dict[str, Any]] = {
    "detect": {
        "auc_diff_ci_lower": -0.004,
        "p_worse": 0.1,
        "new_only_opens": 13,
        "current_only_opens": 9,
    },
    "severity_s3": {"auc": 0.92, "precision": 0.70, "recall": 0.84},
    "deep_dive": {"holdout_corr": 0.35},
    "relation": {"precision": 0.85, "recall": 0.7, "current_recall": 0.65},
    "pair": {
        "same_rate_shift_by_cos_band": [0.02, -0.05],
        "precision": 0.9,
        "current_precision": 0.88,
    },
}


def test_all_pass_when_every_gate_met() -> None:
    ok, results = evaluate_gates(GOOD, current_deep_dive_corr=0.33)
    assert ok
    assert {r.status for r in results} == {PASS}


def test_unmeasured_metric_blocks_the_whole_set() -> None:
    metrics = {k: v for k, v in GOOD.items() if k != "pair"}
    ok, results = evaluate_gates(metrics, current_deep_dive_corr=0.33)
    assert not ok
    assert [r.status for r in results if r.model == "pair"] == [UNMEASURED]


def test_one_failure_blocks_all_five() -> None:
    bad = {**GOOD, "severity_s3": {"auc": 0.92, "precision": 0.60, "recall": 0.84}}
    ok, results = evaluate_gates(bad, current_deep_dive_corr=0.33)
    assert not ok
    assert next(r for r in results if r.model == "severity_s3").status == FAIL


def test_detect_fails_when_new_only_opens_fewer() -> None:
    m = {**GOOD["detect"], "new_only_opens": 5}
    assert gate_detect(m).status == FAIL


def test_detect_fails_on_auc_lower_bound_and_p_worse() -> None:
    assert gate_detect({**GOOD["detect"], "auc_diff_ci_lower": -0.02}).status == FAIL
    assert gate_detect({**GOOD["detect"], "p_worse": 0.2}).status == FAIL


def test_severity_gate_equal_to_baseline_passes() -> None:
    assert gate_severity_s3({"auc": 0.916, "precision": 0.69, "recall": 0.83}).status == PASS


def test_deep_dive_unknown_current_is_unmeasured() -> None:
    ok, results = evaluate_gates(GOOD, current_deep_dive_corr=None)
    assert not ok
    assert next(r for r in results if r.model == "deep_dive").status == UNMEASURED


def test_pair_fails_when_same_rate_halves() -> None:
    m = {**GOOD["pair"], "same_rate_shift_by_cos_band": [-0.3]}
    assert gate_pair(m).status == FAIL


def test_bool_is_not_a_number() -> None:
    assert gate_severity_s3({"auc": True, "precision": 0.7, "recall": 0.9}).status == UNMEASURED


def test_parsers_match_trainer_output() -> None:
    sev = "閾値 0.50 (分割外: 回収 83% 適合率 69% S3 を付けた数 194)"
    assert parse_severity_stdout(sev) == {"recall": 0.83, "precision": 0.69}
    assert parse_relation_stdout("閾値 0.82 (交差検証: 精度 85% 回収 70%)") == {
        "precision": 0.85,
        "recall": 0.70,
    }
    assert parse_deep_dive_stdout("検証: 相関 0.334") == {"holdout_corr": 0.334}
    assert parse_deep_dive_stdout("なし") == {}


def test_real_models_all_retrain_targets_have_a_dependency_row() -> None:
    from pathlib import Path

    report = derive_dependencies(Path("config/models"))
    files = {d.file for d in report.s_derived()}
    assert set(RETRAIN_TARGETS) <= files
    assert not report.unmapped


def test_train_jobs_pass_staging_inputs_and_keep_deep_dive_plain() -> None:
    from pathlib import Path

    from scripts.s_switch_retrain import build_train_jobs

    paths = {k: Path(f"/x/{k}") for k in ("kinds", "axes", "probes", "pair_llm")}
    jobs = {n: argv for n, argv, _, _ in build_train_jobs("s:new", paths, pair_model="s:j")}
    assert set(jobs) == {"detect", "severity_s3", "deep_dive", "relation", "pair"}
    assert "--kinds" not in jobs["deep_dive"]  # 本番の選抜は種別を渡さない
    assert jobs["detect"][jobs["detect"].index("--axes-model") + 1] == "s:new"
    assert jobs["severity_s3"][jobs["severity_s3"].index("--max-missing") + 1] == "2"
    assert "--llm-scores" in jobs["pair"] and "s:j" in jobs["pair"]
    assert all("--out" not in argv for argv in jobs.values())  # 出力先は train が staging で足す


def test_severity_gate_prefers_current_measured_with_same_procedure() -> None:
    # 同じ手順で測った現行 (定数より低い) を基準にすると、現行と同等の新は合格する
    current = {"auc": 0.9158, "precision": 0.686, "recall": 0.831}
    res = gate_severity_s3({**current, "current": current})
    assert res.status == PASS
    assert "同じ手順" in res.detail and res.warning == ""
    worse = gate_severity_s3({"auc": 0.90, "precision": 0.686, "recall": 0.84, "current": current})
    assert worse.status == FAIL


def test_severity_gate_unmeasured_when_kinds_do_not_cover_training_items() -> None:
    m = {"auc": 0.92, "precision": 0.7, "recall": 0.9, "kinds_missing": 165.0}
    assert gate_severity_s3(m).status == UNMEASURED


def test_severity_gate_without_current_uses_constants_and_warns() -> None:
    res = gate_severity_s3({"auc": 0.9158, "precision": 0.686, "recall": 0.831})
    assert res.status == FAIL  # 同じ手順の現行は適合で定数 0.69 に届かない
    assert res.warning


def test_control_job_uses_relabeled_axes_when_given() -> None:
    from pathlib import Path

    from scripts.s_switch_retrain import build_control_job

    _, argv_db, out, _ = build_control_job("s:cur", Path("/x/kinds"))
    assert "--axes" not in argv_db
    assert out.name == "detect_model.json.staging_control"
    _, argv, _, _ = build_control_job("s:cur", Path("/x/kinds"), Path("/x/axes"))
    assert argv[argv.index("--axes") + 1] == "/x/axes"
    assert argv[argv.index("--axes-model") + 1] == "s:cur"


def test_staging_paths_out_dir_redirects_all_four() -> None:
    from pathlib import Path

    from scripts.s_switch_retrain import staging_paths

    paths = staging_paths("kuebiko-sft:s26", Path("/tmp/trial"))
    assert all(p.parent == Path("/tmp/trial") for p in paths.values())
    assert paths["probes"].name == "probe_kuebiko-sft_s26.jsonl"
