"""P0-26: 終着の状態の検査 (D4 §1.4)。検査 4 は 11-28 までは警告だけ (pytest は通す)。

assigned ∪ fallback ⊆ {S 枠, N 枠, 埋込, 明示の外部, 緊急の受け皿 (fallback のみ), 例外 (期限内)}。
"""

from __future__ import annotations

import json
import warnings
from datetime import date
from pathlib import Path

from src.tools import model_registry as reg
from src.tools.model_tiers import BUILTIN_MODEL_TIERS, Tier

_CURRENT = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "model_tiers_current.json").read_text()
)
_EXTERNAL_PREFIXES = ("anthropic:", "claudecode:")
#: この日以後は検査 4 を失敗にする (11-28、旗の切替と同じ日)
ENFORCE_FROM = date(2026, 11, 28)


def _allowed_in_tiers() -> set[str]:
    allowed = set(reg.RESIDENT_SLOTS.values())
    allowed.update(m for m, until in reg.RESIDENT_EXCEPTIONS.items() if date.today() <= until)
    return allowed


def _is_exempt(model: str) -> bool:
    return model.startswith(_EXTERNAL_PREFIXES) or model == _CURRENT["tiers"]["embedding"]


def _violations() -> list[str]:
    allowed = _allowed_in_tiers()
    found: list[str] = []
    assigned = {f"tier:{k}": v for k, v in _CURRENT["tiers"].items()}
    assigned.update({f"step:{k}": v for k, v in _CURRENT["step_overrides"].items()})
    for label, model in assigned.items():
        if not _is_exempt(model) and model not in allowed:
            found.append(f"{label}={model}")
    emergency_ok = allowed | set(reg.EMERGENCY_FALLBACK_MODELS)
    for label, model in _CURRENT["local_fallbacks"].items():
        if not _is_exempt(model) and model not in emergency_ok:
            found.append(f"fallback:{label}={model}")
    return found


def test_builtin_defaults_are_resident_slots_or_embedding() -> None:
    allowed = set(reg.RESIDENT_SLOTS.values()) | {BUILTIN_MODEL_TIERS[Tier.EMBEDDING.value]}

    assert set(BUILTIN_MODEL_TIERS.values()) <= allowed


def test_builtin_maps_tiers_to_slots() -> None:
    assert BUILTIN_MODEL_TIERS["fast"] == reg.RESIDENT_SLOTS["S"]
    assert BUILTIN_MODEL_TIERS["reasoning"] == reg.RESIDENT_SLOTS["S"]
    assert BUILTIN_MODEL_TIERS["dialog"] == reg.RESIDENT_SLOTS["S"]
    assert BUILTIN_MODEL_TIERS["narrative"] == reg.RESIDENT_SLOTS["N"]


def test_all_assigned_models_are_registered() -> None:
    names = list(_CURRENT["tiers"].values()) + list(_CURRENT["step_overrides"].values())
    names += list(_CURRENT["local_fallbacks"].values())
    unregistered = [n for n in names if not _is_exempt(n) and not reg.is_registered(n)]

    assert unregistered == []


def test_current_assignments_stay_within_end_state() -> None:
    violations = _violations()
    if not violations:
        return
    message = "2 枠の外の割当 (11-28 から失敗): " + ", ".join(sorted(violations))
    assert date.today() < ENFORCE_FROM, message
    warnings.warn(message, stacklevel=1)


def test_expired_exceptions_are_reported() -> None:
    expired = [m for m, until in reg.RESIDENT_EXCEPTIONS.items() if date.today() > until]
    if expired:
        warnings.warn(f"常駐の例外が期限切れ: {expired}", stacklevel=1)
