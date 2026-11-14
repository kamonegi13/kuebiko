"""深刻度の軸: 被害ありなのに recoverability=not_applicable の矛盾の扱い。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from src.cti import severity_axes as sa
from src.cti.severity_axes import SeverityAxes

_DAMAGE = ["unauthorized_access", "data_exposure", "disruption", "destructive"]
_NO_DAMAGE = ["none", "attempted"]


def _axes(impact: str, recov: str | None = "not_applicable") -> SeverityAxes:
    return SeverityAxes.model_validate(
        {
            "scope": "single_org",
            "impact": impact,
            "confirmation": "confirmed",
            "magnitude": "unknown",
            "exploitation": "not_applicable",
            "actor": "unknown",
            "target": "general_org",
            "recoverability": recov,
        }
    )


@pytest.mark.parametrize("impact", _DAMAGE)
def test_damage_with_not_applicable_becomes_unknown(impact: str) -> None:
    original = _axes(impact)
    fixed, changed = sa.normalize_recoverability(original)
    assert changed is True
    assert fixed.recoverability == "unknown"
    assert original.recoverability == "not_applicable"  # 元は不変


@pytest.mark.parametrize("impact", _NO_DAMAGE)
def test_no_damage_keeps_not_applicable(impact: str) -> None:
    axes = _axes(impact)
    fixed, changed = sa.normalize_recoverability(axes)
    assert changed is False
    assert fixed is axes


@pytest.mark.parametrize("recov", ["regular", "extended", "not_recoverable", "unknown", None])
def test_other_recoverability_untouched(recov: str | None) -> None:
    _, changed = sa.normalize_recoverability(_axes("disruption", recov))
    assert changed is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "shadow"), ("0", "0"), ("1", "1"), ("shadow", "shadow"), ("bogus", "shadow")],
)
def test_flag_parsing(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: str) -> None:
    if raw is None:
        monkeypatch.delenv("AXES_RECOVERABILITY_FIX", raising=False)
    else:
        monkeypatch.setenv("AXES_RECOVERABILITY_FIX", raw)
    assert sa.recoverability_fix_mode() == expected


def test_shadow_logs_without_changing_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXES_RECOVERABILITY_FIX", "shadow")
    axes = _axes("data_exposure")
    with capture_logs() as logs:
        out = sa.apply_recoverability_fix(axes, "a1")
    assert out is axes
    events = [e for e in logs if e["event"] == "axes_recoverability_inconsistent"]
    assert len(events) == 1
    assert events[0]["impact"] == "data_exposure"
    assert events[0]["article_id"] == "a1"


def test_on_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXES_RECOVERABILITY_FIX", "1")
    out = sa.apply_recoverability_fix(_axes("destructive"))
    assert out is not None
    assert out.recoverability == "unknown"


def test_off_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXES_RECOVERABILITY_FIX", "0")
    axes = _axes("destructive")
    with capture_logs() as logs:
        assert sa.apply_recoverability_fix(axes) is axes
    assert logs == []


def test_none_passes_through() -> None:
    assert sa.apply_recoverability_fix(None) is None


@pytest.mark.asyncio
async def test_classify_axes_applies_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXES_RECOVERABILITY_FIX", "1")
    llm: Any = AsyncMock()
    llm.generate_structured.return_value = _axes("disruption")
    out = await sa.classify_axes(llm, "t", "s")
    assert out is not None
    assert out.recoverability == "unknown"
