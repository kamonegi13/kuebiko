"""P0-27: 登録簿。設定で割り当てたモデルは登録済みでなければならない。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools import model_registry as reg

_CURRENT = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "model_tiers_current.json").read_text()
)
_EXTERNAL_PREFIXES = ("anthropic:", "claudecode:")
_EMBEDDING_TIER = "embedding"


def _assigned_models() -> set[str]:
    values: set[str] = set()
    for tier, model in _CURRENT["tiers"].items():
        if tier != _EMBEDDING_TIER:
            values.add(model)
    values.update(_CURRENT["step_overrides"].values())
    values.update(_CURRENT["local_fallbacks"].values())
    return {m for m in values if not m.startswith(_EXTERNAL_PREFIXES)}


@pytest.mark.parametrize("model", sorted(_assigned_models()))
def test_assigned_local_model_is_registered(model: str) -> None:
    assert reg.is_registered(model), f"{model} を model_registry に登録する (1 行)"


def test_unregistered_model_is_reported() -> None:
    assert not reg.is_registered("kuebiko-sft:n20")
    assert not reg.is_registered("kuebiko-sft:s26")


def test_prefix_family_registration_covers_interpolation_tags() -> None:
    assert reg.is_registered("kuebiko-sft:s24ci65")
    assert reg.is_registered("kuebiko-sft:s25i65")


def test_resident_slot_models_are_registered() -> None:
    for model in reg.RESIDENT_SLOTS.values():
        assert reg.is_registered(model)


def test_longest_prefix_wins() -> None:
    cap = reg.find_capability("kuebiko-sft:s24c")
    assert cap is not None and cap.focus_v6 is True


def test_emergency_fallback_is_registered_and_not_resident_slot() -> None:
    for model in reg.EMERGENCY_FALLBACK_MODELS:
        assert reg.is_registered(model)
        assert model not in reg.RESIDENT_SLOTS.values()
