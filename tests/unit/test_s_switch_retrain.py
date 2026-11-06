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
