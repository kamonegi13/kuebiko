"""P0-27: 登録簿への集約で、全モデル x 全 step の判定が移す前と同じであること。

``tests/fixtures/model_registry_golden.json`` は移す前の定数・関数から採った記録。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.digest import focus_v6
from src.pipeline import summary
from src.tools import task_prefix as tp
from src.tools.decision_probe import _is_json_biased
from src.tools.model_tiers import Step

_GOLDEN = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "model_registry_golden.json").read_text()
)
_CONSTANTS = _GOLDEN.pop("__constants__")
_STEPS = list(Step)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SFT_TASK_PREFIX_MODELS", "PIR_FOCUS_PROMPT_V6", "SFT_EVIDENCE_MODELS"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("model", sorted(_GOLDEN))
def test_model_answers_match_pre_registry_behavior(model: str) -> None:
    expected = _GOLDEN[model]

    assert tp.is_prefix_trained(model) is expected["is_prefix_trained"]
    assert tp.task_prefix_enabled(model) is expected["task_prefix_enabled"]
    assert _is_json_biased(model) is expected["json_biased"]
    assert sorted(s.value for s in tp.untrained_steps_for(model)) == expected["untrained"]
    assert summary.summary_schema_for(model).__name__ == expected["evidence"]
    assert focus_v6.is_enabled(model) is expected["v6"]
    assert {s.value: tp.prefix_for(s, model) for s in _STEPS} == expected["prefix_by_step"]
    assert {s.value: tp.with_task_prefix("X", s, model) for s in _STEPS} == expected["with_prefix"]


def test_exported_constants_match_pre_registry_values() -> None:
    assert sorted(tp.PREFIX_TRAINED_MODELS) == _CONSTANTS["PREFIX_TRAINED_MODELS"]
    assert sorted(tp.PREFIX_TRAINED_FAMILIES) == _CONSTANTS["PREFIX_TRAINED_FAMILIES"]
    assert sorted(tp.S24_NEW_TASK_FAMILIES) == _CONSTANTS["S24_NEW_TASK_FAMILIES"]
    assert {k: sorted(s.value for s in v) for k, v in tp.UNTRAINED_STEPS.items()} == _CONSTANTS[
        "UNTRAINED_STEPS"
    ]
    assert sorted(summary.EVIDENCE_TRAINED_MODELS) == _CONSTANTS["EVIDENCE_MODELS"]
    assert sorted(summary.EVIDENCE_TRAINED_FAMILIES) == _CONSTANTS["EVIDENCE_FAMILIES"]
    assert sorted(focus_v6.V6_MODEL_FAMILIES) == _CONSTANTS["V6_MODEL_FAMILIES"]
