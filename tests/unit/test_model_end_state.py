"""P0-26: 終着の状態の検査 (D4 §1.4)。pytest は警告だけ・静的な写しに頼らない。

本番の割当の検査は ``scripts/check_model_end_state.py`` と health-status の
``model_end_state`` が本番の DB を相手に行う。ここは検査ロジックと既定値を固める。
"""

from __future__ import annotations

import warnings
from datetime import date, timedelta

from src.tools import model_registry as reg
from src.tools.model_end_state import find_violations
from src.tools.model_tiers import BUILTIN_MODEL_TIERS, Tier

_S = reg.RESIDENT_SLOTS["S"]
_N = reg.RESIDENT_SLOTS["N"]
_BEFORE = date(2026, 11, 1)
_TWO_SLOT = reg.RESIDENT_EXCEPTIONS["kuebiko-sft:s23i"]
_TRANSLATION = reg.RESIDENT_EXCEPTIONS["kuebiko-sft:26b"]
_AFTER = _TRANSLATION + timedelta(days=1)
_TIERS = {"fast": _S, "reasoning": _S, "narrative": _N, "dialog": "claudecode:haiku"}


def test_builtin_defaults_are_slots_embedding_or_dialog_emergency() -> None:
    allowed = set(reg.RESIDENT_SLOTS.values()) | {
        BUILTIN_MODEL_TIERS[Tier.EMBEDDING.value],
        reg.DIALOG_LOCAL_DEFAULT,
    }

    assert set(BUILTIN_MODEL_TIERS.values()) <= allowed


def test_builtin_maps_tiers_to_slots_and_dialog_stays_emergency() -> None:
    assert BUILTIN_MODEL_TIERS["fast"] == _S
    assert BUILTIN_MODEL_TIERS["reasoning"] == _S
    assert BUILTIN_MODEL_TIERS["narrative"] == _N
    # TODO: s24ci65 が dialog の 4 step で形を保つと確かめてから S にする (別作業)
    assert BUILTIN_MODEL_TIERS["dialog"] == "gemma4:26b"
    assert BUILTIN_MODEL_TIERS["dialog"] in reg.EMERGENCY_FALLBACK_MODELS


def test_dialog_fallback_is_emergency_model_not_slot() -> None:
    from src.tools.model_tiers import resolve_local_fallback_model

    assert resolve_local_fallback_model(Tier.DIALOG) == "gemma4:26b"
    assert resolve_local_fallback_model(Tier.FAST) == _S
    assert resolve_local_fallback_model(Tier.NARRATIVE) == _N


def test_clean_assignment_has_no_violations() -> None:
    assert find_violations(_TIERS, {"triage": _S}, {"fast": _S}, today=_BEFORE) == []


def test_gemma_in_step_override_is_violation_except_listed_steps_until_deadline() -> None:
    steps = {"triage": "gemma4:26b", "assign_evidence_judge": "gemma4:26b"}

    found = find_violations(_TIERS, steps, {}, today=_BEFORE)

    assert len(found) == 1 and found[0].startswith("step:triage=")


def test_step_exceptions_expire() -> None:
    steps = {"org_identity_judge": "gemma4:26b", "pair_judge": "kuebiko-sft:s23i"}

    found = find_violations(_TIERS, steps, {}, today=_AFTER)

    assert len(found) == 2 and all("期限切れ" in f for f in found)


def test_emergency_model_allowed_only_in_fallback_keys() -> None:
    assert find_violations(_TIERS, {}, {"fast": "gemma4:26b"}, today=_AFTER) == []
    assert find_violations({**_TIERS, "fast": "gemma4:26b"}, {}, {}, today=_AFTER) != []


def test_unregistered_model_is_violation() -> None:
    found = find_violations(_TIERS, {"triage": "kuebiko-sft:n20"}, {}, today=_BEFORE)

    assert len(found) == 1 and "未登録" in found[0]


def test_expired_exceptions_only_warn() -> None:
    # 固定の日付で試す。「今日」に依る検査は運用の check (check_live_assignments) の仕事
    expired = [m for m, until in reg.RESIDENT_EXCEPTIONS.items() if until < _AFTER]
    assert set(expired) == set(reg.RESIDENT_EXCEPTIONS)
    warnings.warn(f"常駐の例外が期限切れ (固定日 {_AFTER}): {expired}", stacklevel=1)


def test_exception_flips_exactly_at_deadline() -> None:
    tiers = {**_TIERS, "fast": "kuebiko-sft:s23i"}

    assert find_violations(tiers, {}, {}, today=_TWO_SLOT) == []
    assert find_violations(tiers, {}, {}, today=_TWO_SLOT + timedelta(days=1)) != []


def test_exception_deadlines_are_split_per_model() -> None:
    two_slot = date(2027, 1, 10)
    translation = date(2027, 3, 23)

    assert reg.RESIDENT_EXCEPTIONS["kuebiko-sft:s23i"] == two_slot
    assert reg.RESIDENT_EXCEPTIONS["kuebiko-sft:26b"] == translation
    assert reg.RESIDENT_STEP_EXCEPTIONS[("assign_evidence_judge", "gemma4:26b")] == two_slot
    assert reg.RESIDENT_STEP_EXCEPTIONS[("org_identity_judge", "gemma4:26b")] == two_slot


def test_translation_model_outlives_two_slot_deadline() -> None:
    tiers = {**_TIERS, "fast": "kuebiko-sft:26b"}
    between = _TWO_SLOT + timedelta(days=30)

    assert find_violations(tiers, {}, {}, today=between) == []
    assert find_violations(tiers, {}, {}, today=_AFTER) != []
