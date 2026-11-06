"""S 切替の指標の計算 (src/tools/s_switch_measure.py) と kinds_model の記録・3 腕の関門。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from src.tools.ml_s_dependency import derive_dependencies, find_misaligned
from src.tools.s_switch_gate import (
    CONFOUNDED_WARNING,
    FAIL,
    PASS,
    UNMEASURED,
    gate_detect,
)
from src.tools.s_switch_measure import (
    auc_score,
    band_same_rates,
    compare_fold_aucs,
    merge_metrics,
    precision_recall_at,
    same_rate_shifts,
)
from src.tools.staging_inputs import infer_kinds_model, resolve_kinds_model

# --- 純粋な指標 ---


def test_auc_perfect_inverse_and_ties() -> None:
    assert auc_score([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert auc_score([0, 0, 1, 1], [0.9, 0.8, 0.2, 0.1]) == 0.0
    assert auc_score([0, 1], [0.5, 0.5]) == 0.5  # 同値は平均順位


def test_auc_without_both_classes_is_nan() -> None:
    assert np.isnan(auc_score([1, 1], [0.2, 0.9]))


def test_precision_recall_at_fixed_threshold() -> None:
    precision, recall = precision_recall_at([1, 1, 0, 0], [0.9, 0.4, 0.6, 0.1], 0.5)
    assert (precision, recall) == (0.5, 0.5)
    assert precision_recall_at([1, 0], [0.1, 0.2], 0.5) == (0.0, 0.0)  # 付けた数 0


def test_compare_fold_aucs_clear_improvement_and_clear_loss() -> None:
    pytest.importorskip("scipy")
    base = [0.80 + 0.001 * i for i in range(10)]
    better = [v + 0.05 for v in base]
    noisy = [v + d for v, d in zip(better, [0.001, -0.001] * 5, strict=True)]
    up = compare_fold_aucs(base, noisy, test_train_ratio=0.25)
    assert up["ci_lower"] > 0.0 and up["p_worse"] < 0.01 and up["p_better"] > 0.9
    worse = [v - d for v, d in zip(base, [0.05 + 0.001, 0.05 - 0.001] * 5, strict=True)]
    down = compare_fold_aucs(base, worse, test_train_ratio=0.25)
    assert down["ci_lower"] < -0.01 and down["p_worse"] > 0.9


def test_compare_fold_aucs_zero_variance_and_bad_input() -> None:
    pytest.importorskip("scipy")
    same = compare_fold_aucs([0.9, 0.9, 0.9], [0.9, 0.9, 0.9], test_train_ratio=0.25)
    assert same["p_rope"] == 1.0 and same["mean_diff"] == 0.0
    with pytest.raises(ValueError):
        compare_fold_aucs([0.9], [0.9], test_train_ratio=0.25)
    with pytest.raises(ValueError):
        compare_fold_aucs([0.9, 0.8], [0.9], test_train_ratio=0.25)


def test_band_same_rates_and_shifts_skip_small_bands() -> None:
    cos = [0.65] * 10 + [0.95] * 3
    new = [True] * 5 + [False] * 5 + [True] * 3
    cur = [True] * 8 + [False] * 2 + [False] * 3
    rates = band_same_rates(cos, new)
    assert rates[1] == (10, 0.5) and rates[4] == (3, 1.0) and rates[0] == (0, None)
    # 0.6-0.7 の帯 |0.5-0.8| = 0.3 だけ。0.9 以上の帯は 3 対 (< 10) で比べない
    assert same_rate_shifts(cos, new, cur) == pytest.approx([0.3])


def test_merge_metrics_keeps_manual_values_when_computed_is_none() -> None:
    existing: dict[str, Any] = {
        "detect": {"new_vs_control": {"new_only_opens": 13, "p_worse": 0.5}},
        "x": 1,
    }
    computed = {"detect": {"new_vs_control": {"new_only_opens": None, "p_worse": 0.1}}, "y": None}
    merged: Any = merge_metrics(existing, computed)
    assert merged["detect"]["new_vs_control"] == {"new_only_opens": 13, "p_worse": 0.1}
    assert merged["x"] == 1 and merged["y"] is None
    assert existing["detect"]["new_vs_control"]["p_worse"] == 0.5  # 元は書き換えない


# --- 3 腕の関門 ---

ARM: dict[str, Any] = {
    "auc_diff_ci_lower": -0.004,
    "p_worse": 0.1,
    "new_only_opens": 13,
    "control_only_opens": 9,
}


def test_three_arm_gate_uses_new_vs_control_and_reports_control_as_info() -> None:
    bad_info = {"auc_diff_ci_lower": -0.2, "p_worse": 0.9, "mean_diff": -0.1}
    res = gate_detect({"new_vs_control": ARM, "control_vs_current": bad_info})
    assert res.status == PASS  # 対照 vs 現行が悪くても合否に使わない
    assert "合否に使わない" in res.warning and "-0.100" in res.warning


def test_three_arm_gate_fails_when_new_loses_to_control() -> None:
    res = gate_detect({"new_vs_control": {**ARM, "control_only_opens": 20}})
    assert res.status == FAIL and "対照" in res.detail


def test_open_counts_null_stay_unmeasured_with_a_hint() -> None:
    arm = {**ARM, "new_only_opens": None, "control_only_opens": None}
    res = gate_detect({"new_vs_control": arm})
    assert res.status == UNMEASURED
    assert "new_only_opens" in res.detail and "digest" in res.detail


def test_without_control_gate_behaves_as_before_but_warns() -> None:
    flat = {**{k: v for k, v in ARM.items() if k != "control_only_opens"}, "current_only_opens": 9}
    res = gate_detect(flat)
    assert res.status == PASS and res.warning == CONFOUNDED_WARNING
    assert gate_detect({**flat, "p_worse": 0.5}).status == FAIL


# --- kinds_model の記録 ---


def _kinds(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    p = tmp_path / "k.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def test_infer_kinds_model_single_mixed_and_missing(tmp_path: Path) -> None:
    one = _kinds(tmp_path, [{"article_id": "a", "kind": "x", "model": "s:new"}] * 2)
    assert infer_kinds_model(one) == "s:new"
    mixed = _kinds(
        tmp_path,
        [
            {"article_id": "a", "kind": "x", "model": "s:old"},
            {"article_id": "b", "kind": "x", "model": "s:new"},
        ],
    )
    assert infer_kinds_model(mixed) is None
    bare = _kinds(tmp_path, [{"article_id": "a", "kind": "x"}])
    assert infer_kinds_model(bare) is None


def test_resolve_kinds_model_prefers_explicit_and_skips_db_path(tmp_path: Path) -> None:
    f = _kinds(tmp_path, [{"article_id": "a", "kind": "x", "model": "s:file"}])
    assert resolve_kinds_model("s:cli", f) == "s:cli"
    assert resolve_kinds_model(None, f) == "s:file"
    assert resolve_kinds_model(None, None) is None  # DB・既存ファイル = 記録なし


@pytest.mark.parametrize(
    ("name", "feature", "field"),
    [
        ("detect_model.json", "kind=breach", "kinds_model"),
        ("deep_dive_model.json", "kind=breach", "kinds_model"),
        ("severity_s3_model.json", "kind=breach", "kinds_model"),
        ("relation_model.json", "kinds_known", "kinds_model"),
        ("pair_model.json", "kind_same", "kinds_model"),
    ],
)
def test_kinds_model_counts_as_a_record(
    tmp_path: Path, name: str, feature: str, field: str
) -> None:
    body: dict[str, Any] = {"feature_names": [feature]}
    (tmp_path / name).write_text(json.dumps(body), encoding="utf-8")
    [m] = find_misaligned(derive_dependencies(tmp_path), "s-new")
    assert m.reason == "unrecorded"  # 欄なし
    (tmp_path / name).write_text(json.dumps({**body, field: "s-old"}), encoding="utf-8")
    [m] = find_misaligned(derive_dependencies(tmp_path), "s-new")
    assert (m.reason, m.trained_models) == ("mismatch", ("s-old",))
    assert find_misaligned(derive_dependencies(tmp_path), "s-old") == ()
    (tmp_path / name).write_text(json.dumps({**body, field: None}), encoding="utf-8")
    [m] = find_misaligned(derive_dependencies(tmp_path), "s-new")
    assert m.reason == "unrecorded"  # null = 記録なし


def test_loaders_tolerate_kinds_model_field(tmp_path: Path) -> None:
    from src.synthesis.grounded.detect_features import FEATURE_NAMES
    from src.synthesis.grounded.detect_ml import load_detect_model

    n = len(FEATURE_NAMES)
    base = {
        "feature_names": list(FEATURE_NAMES),
        "mean": [0.0] * n,
        "scale": [1.0] * n,
        "coef": [0.0] * n,
        "intercept": 0.0,
        "threshold": 0.5,
        "axes_model": "s",
    }
    for extra in ({}, {"kinds_model": "s"}, {"kinds_model": None}):
        p = tmp_path / "d.json"
        p.write_text(json.dumps({**base, **extra}), encoding="utf-8")
        assert load_detect_model(p) is not None
