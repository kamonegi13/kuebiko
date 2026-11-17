"""地政学の救済の問いのしきい値をモデルごとに持つ (s26r 系 0.3、ほかは従来の 0.5)。"""

from __future__ import annotations

from typing import Any

import pytest

from src.pipeline import geo_rescue_judge as gj


def _res(p: float, threshold: float) -> gj.GeoJudgeResult:
    return gj.GeoJudgeResult(
        cues=("nation:RU",), p_watched_action=p, p_allied_response=0.0, threshold=threshold
    )


def test_default_threshold_is_unchanged() -> None:
    assert gj.threshold_for_model("kuebiko-sft:26b") == gj.GEO_JUDGE_THRESHOLD == 0.5
    assert gj.threshold_for_model("gemma4:26b") == 0.5


def test_s26r_family_uses_its_threshold() -> None:
    assert gj.threshold_for_model("kuebiko-sft:s26r") == 0.3
    assert gj.threshold_for_model("kuebiko-sft:s26r2") == 0.3


def test_result_uses_its_threshold() -> None:
    assert _res(0.35, 0.3).passed is True
    assert _res(0.35, 0.5).passed is False
    assert (
        gj.GeoJudgeResult(cues=("nation:RU",), p_watched_action=0.45, p_allied_response=None).passed
        is False
    )


@pytest.mark.asyncio
async def test_judge_resolves_threshold_from_assigned_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_probe(*_a: Any, **_k: Any) -> dict[str, float]:
        return {"Yes": 0.35, "No": 0.65}

    monkeypatch.setattr(gj, "probe_choice", fake_probe)
    monkeypatch.setattr(gj, "resolve_step_model", lambda *_a, **_k: "kuebiko-sft:s26r")
    res = await gj.judge_geo_rescue("Russia drills", "Russian forces held exercises.", config=None)  # type: ignore[arg-type]
    if res.has_cue:
        assert res.threshold == 0.3
