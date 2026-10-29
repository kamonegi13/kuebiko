"""src.tools.probe_thresholds と config/models/probe_thresholds.json のテスト (s24_plan §4.2-7)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools.model_tiers import BUILTIN_MODEL_TIERS, STEP_REGISTRY, Step, Tier
from src.tools.probe_thresholds import (
    CURRENT_PRODUCTION_CONSTANTS,
    PROBE_STEP_QUESTIONS,
    THRESHOLDS_PATH,
    load_thresholds,
    merged,
    missing_entries,
    probe_threshold,
    save_thresholds,
)

#: 本番で logprob 判定に使われている/使われた実モデル (戻し先を含む)
DEPLOYED_MODELS = ("gemma4:26b", "kuebiko-sft:s23i")


def test_every_probe_step_is_fast_tier() -> None:
    # BUILTIN の解決先 (fast) が表に項を持つこと
    for step in PROBE_STEP_QUESTIONS:
        assert STEP_REGISTRY[step].tier is Tier.FAST


#: P0-26 で BUILTIN の fast が s24ci65 になった。s24ci65 の較正は一部の probe しか無い
#: (較正の追加は P0-04 の範囲)。穴が埋まれば xfail は XPASS で気付ける (strict)。
_BUILTIN_FAST_GAP = pytest.param(
    BUILTIN_MODEL_TIERS[Tier.FAST.value],
    marks=pytest.mark.xfail(strict=True, reason="s24ci65 の probe 較正が未完 (P0-04)"),
)


@pytest.mark.parametrize("model", [*DEPLOYED_MODELS, _BUILTIN_FAST_GAP])
def test_deployed_models_have_all_four_probes(model: str) -> None:
    assert missing_entries(model, load_thresholds()) == []


def test_seed_entries_equal_current_production_constants() -> None:
    table = load_thresholds()
    for model in DEPLOYED_MODELS:
        for step, questions in CURRENT_PRODUCTION_CONSTANTS.items():
            assert table[model][step.value] == dict(questions)


def test_seed_matches_code_constants() -> None:
    from src.digest.pir_daily_focus import PIR_FOCUS_CLAIM_THRESHOLD
    from src.eventnews.org_identity import EVENT_MIN_LOGIT, ORG_MIN_LOGIT
    from src.pipeline.briefing import THIN_BODY_PROBE_THRESHOLD

    assert CURRENT_PRODUCTION_CONSTANTS[Step.THIN_BODY_JUDGE]["main"] == THIN_BODY_PROBE_THRESHOLD
    assert (
        CURRENT_PRODUCTION_CONSTANTS[Step.PIR_FOCUS_CLAIM_JUDGE]["main"]
        == PIR_FOCUS_CLAIM_THRESHOLD
    )
    assert CURRENT_PRODUCTION_CONSTANTS[Step.ORG_IDENTITY_JUDGE]["event_logit"] == EVENT_MIN_LOGIT
    assert CURRENT_PRODUCTION_CONSTANTS[Step.ORG_IDENTITY_JUDGE]["org_logit"] == ORG_MIN_LOGIT


def test_unknown_model_is_fail_closed() -> None:
    assert probe_threshold("kuebiko-sft:unknown", Step.THIN_BODY_JUDGE) is None
    assert missing_entries("kuebiko-sft:unknown", load_thresholds()) != []


def test_known_model_returns_value() -> None:
    assert probe_threshold("gemma4:26b", Step.THIN_BODY_JUDGE) == 0.075
    assert probe_threshold(" gemma4:26b ", Step.ORG_IDENTITY_JUDGE, "event_logit") == 12.0
    assert probe_threshold("gemma4:26b", Step.ORG_IDENTITY_JUDGE, "nope") is None


def test_missing_or_broken_file_is_empty(tmp_path: Path) -> None:
    assert load_thresholds(tmp_path / "none.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_thresholds(bad) == {}
    bad.write_text("[1,2]", encoding="utf-8")
    assert load_thresholds(bad) == {}


def test_non_numeric_values_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text(
        json.dumps({"m": {"thin_body_judge": {"main": "0.1", "x": True, "y": 0.2, "z": None}}}),
        encoding="utf-8",
    )
    assert load_thresholds(path) == {"m": {"thin_body_judge": {"y": 0.2}}}


def test_merged_keeps_other_models_and_steps_and_save_roundtrips(tmp_path: Path) -> None:
    base = load_thresholds()
    new = merged(base, "kuebiko-sft:s24", Step.THIN_BODY_JUDGE, {"main": 0.09})
    assert new["gemma4:26b"] == base["gemma4:26b"]  # 戻し先の項は消さない
    assert base.get("kuebiko-sft:s24") is None  # 元は変えない
    path = tmp_path / "t.json"
    save_thresholds(new, path)
    assert load_thresholds(path) == new


def test_default_path_is_inside_repo() -> None:
    assert THRESHOLDS_PATH.name == "probe_thresholds.json"
    assert THRESHOLDS_PATH.exists()
