"""S 由来の特徴の一覧 (src/tools/ml_s_dependency.py) の不変条件。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from src.tools.ml_s_dependency import (
    NOT_RETRAINED,
    RETRAIN_PLAN,
    RETRAIN_TARGETS,
    STRONG,
    derive_dependencies,
    find_misaligned,
)

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "config" / "models"


def _write(dir_: Path, name: str, body: Mapping[str, object]) -> None:
    (dir_ / name).write_text(json.dumps(body), encoding="utf-8")


def test_every_shipped_model_feature_is_mapped() -> None:
    report = derive_dependencies(MODELS)
    assert report.unmapped == ()
    assert report.unknown_files == ()


def test_retrain_targets_all_have_strong_s_derived_features() -> None:
    report = derive_dependencies(MODELS)
    strong = set(report.files_with_s_derived(strength=STRONG))
    assert set(RETRAIN_TARGETS) <= strong


def test_non_targets_have_no_strong_s_derived_features() -> None:
    report = derive_dependencies(MODELS)
    strong = set(report.files_with_s_derived(strength=STRONG))
    assert strong.isdisjoint(NOT_RETRAINED)


def test_retrain_plan_covers_targets_and_trainers_exist() -> None:
    assert {s.file for s in RETRAIN_PLAN} == set(RETRAIN_TARGETS)
    for step in RETRAIN_PLAN:
        if step.trainer is not None:
            assert (ROOT / step.trainer).is_file(), step.trainer


def test_unmapped_feature_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, "relation_model.json", {"feature_names": ["kinds_known", "brand_new_feature"]})
    report = derive_dependencies(tmp_path)
    assert [u.feature for u in report.unmapped] == ["brand_new_feature"]


def test_unknown_json_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, "mystery_model.json", {"feature_names": ["x"]})
    assert derive_dependencies(tmp_path).unknown_files == ("mystery_model.json",)


def test_staging_and_backup_files_are_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "detect_model.json.staging", {"feature_names": ["zzz"]})
    _write(tmp_path, "pair_model.v1.bak.json", {"feature_names": ["zzz"]})
    report = derive_dependencies(tmp_path)
    assert report.dependencies == () and report.unknown_files == ()


def test_mismatch_when_trained_model_differs_from_current_s(tmp_path: Path) -> None:
    _write(
        tmp_path, "detect_model.json", {"axes_model": "s-old", "feature_names": ["axis_scope=a"]}
    )
    report = derive_dependencies(tmp_path)
    [m] = find_misaligned(report, "s-new")
    assert (m.file, m.reason, m.trained_models) == ("detect_model.json", "mismatch", ("s-old",))
    assert find_misaligned(report, "s-old") == ()


def test_unrecorded_when_model_keeps_no_trained_field(tmp_path: Path) -> None:
    _write(tmp_path, "relation_model.json", {"feature_names": ["kinds_known"]})
    [m] = find_misaligned(derive_dependencies(tmp_path), "s-new")
    assert m.reason == "unrecorded"


def test_pair_judge_list_membership(tmp_path: Path) -> None:
    body = {"feature_names": ["llm_same"], "trained_llm_models": ["s-a", "s-b"]}
    _write(tmp_path, "pair_model.json", body)
    report = derive_dependencies(tmp_path)
    assert find_misaligned(report, "s-b") == ()
    assert find_misaligned(report, "s-c")[0].reason == "mismatch"
