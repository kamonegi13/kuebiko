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


@pytest.mark.asyncio
async def test_emergency_record_posts_ops_notice_once_a_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    sent: list[str] = []

    async def _fake_post(*, title: str, body: str, importance: str = "low", **_: object) -> bool:
        sent.append(title)
        return True

    monkeypatch.setattr("src.ui.services.ops_notify.post_ops_message", _fake_post)

    ef.record_emergency_fallback(step="a", original="x", fallback=_EMERGENCY, reason="r")
    ef.record_emergency_fallback(step="b", original="x", fallback=_EMERGENCY, reason="r")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(sent) == 1
    assert ef.emergency_fallback_counts() == {_EMERGENCY: 2}


def test_eval_allow_env_permits_unregistered_override_without_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.tools.model_registry import is_override_allowed
    from src.tools.task_prefix import is_prefix_trained

    monkeypatch.setenv("MODEL_EVAL_ALLOW", "foo:eval1, bar:eval2")

    assert is_override_allowed("foo:eval1")
    assert is_override_allowed("bar:eval2")
    assert not is_override_allowed("baz:1")
    assert not is_prefix_trained("foo:eval1")


def test_step_exception_allows_assign_and_org_gemma_in_validate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_RESIDENT_ENFORCE", "1")
    tiers = _tiers(
        **{"step:assign_evidence_judge": _EMERGENCY, "step:org_identity_judge": _EMERGENCY}
    )

    assert validate_model_tiers(tiers) == []
