"""P0-26: 緊急の受け皿は明示・記録・計数つきで、黙って落ちない。"""

from __future__ import annotations

from datetime import date

import pytest

from src.tools import emergency_fallback as ef
from src.tools.model_registry import RESIDENT_SLOTS
from src.tools.model_tiers import validate_model_tiers

_S = RESIDENT_SLOTS["S"]
_N = RESIDENT_SLOTS["N"]
_EMERGENCY = "gemma4:26b"


@pytest.fixture(autouse=True)
def _reset() -> None:
    ef.reset_for_tests()


def test_preferred_is_kept_when_installed() -> None:
    chosen = ef.pick_installed_or_emergency(_S, [_S, _EMERGENCY], step="x", reason="r")

    assert chosen == _S
    assert ef.emergency_fallback_counts() == {}


def test_latest_suffix_is_treated_as_installed() -> None:
    assert ef.pick_installed_or_emergency(_S, [f"{_S}:latest"], step="x", reason="r") == _S


def test_falls_to_emergency_only_when_preferred_missing_and_counts() -> None:
    chosen = ef.pick_installed_or_emergency(_S, [_EMERGENCY], step="triage", reason="missing")

    assert chosen == _EMERGENCY
    assert ef.emergency_fallback_counts() == {_EMERGENCY: 1}


def test_stays_on_preferred_when_nothing_installed() -> None:
    assert ef.pick_installed_or_emergency(_S, [], step="x", reason="r") == _S
    assert ef.emergency_fallback_counts() == {}


def test_missing_resident_models_reports_absent_slots() -> None:
    assert ef.missing_resident_models([_S]) == [_N]
    assert ef.missing_resident_models([_S, _N]) == []


def test_ops_notice_once_per_day() -> None:
    day = date(2026, 11, 1)

    assert ef.should_notify_ops_today(day) is True
    assert ef.should_notify_ops_today(day) is False
    assert ef.should_notify_ops_today(date(2026, 11, 2)) is True


def test_fallback_client_records_emergency_when_fallback_arm_is_gemma() -> None:
    from src.tools.llm_fallback import FallbackLLMClient

    class _Stub:
        def __init__(self, model: str) -> None:
            self.model = model

    client = FallbackLLMClient(_Stub("claudecode:haiku"), _Stub(_EMERGENCY))  # type: ignore[arg-type]
    client._enter_cooldown(RuntimeError("down"))  # noqa: SLF001

    assert ef.emergency_fallback_counts() == {_EMERGENCY: 1}
    FallbackLLMClient.reset_cooldowns()


def test_fallback_client_does_not_record_for_resident_fallback() -> None:
    from src.tools.llm_fallback import FallbackLLMClient

    class _Stub:
        def __init__(self, model: str) -> None:
            self.model = model

    client = FallbackLLMClient(_Stub("claudecode:haiku"), _Stub(_S))  # type: ignore[arg-type]
    client._enter_cooldown(RuntimeError("down"))  # noqa: SLF001

    assert ef.emergency_fallback_counts() == {}
    FallbackLLMClient.reset_cooldowns()


def _tiers(**extra: str) -> dict[str, str]:
    base = {"fast": _S, "reasoning": _S, "dialog": _S, "narrative": _N}
    return {**base, "embedding": "snowflake-arctic-embed2", **extra}


def test_enforce_rejects_emergency_in_step_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_RESIDENT_ENFORCE", "1")

    errs = validate_model_tiers(_tiers(**{"step:triage": _EMERGENCY}))

    assert any("triage" in e for e in errs)


def test_enforce_allows_emergency_in_fallback_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_RESIDENT_ENFORCE", "1")

    assert validate_model_tiers(_tiers(**{"fallback:fast": _EMERGENCY})) == []


def test_warning_mode_accepts_out_of_slot_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_RESIDENT_ENFORCE", raising=False)
    monkeypatch.delenv("MODEL_REGISTRY_ENFORCE", raising=False)

    assert validate_model_tiers(_tiers(**{"step:triage": _EMERGENCY, "fast": "foo:1"})) == []


def test_registry_enforce_rejects_unregistered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_REGISTRY_ENFORCE", "1")

    errs = validate_model_tiers(_tiers(**{"step:triage": "kuebiko-sft:n20"}))

    assert any("未登録" in e for e in errs)


def test_enforce_still_accepts_resident_and_external(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_RESIDENT_ENFORCE", "1")
    monkeypatch.setenv("MODEL_REGISTRY_ENFORCE", "1")
    tiers = _tiers(**{"dialog": "claudecode:haiku", "step:triage": _S})

    assert validate_model_tiers(tiers) == []
