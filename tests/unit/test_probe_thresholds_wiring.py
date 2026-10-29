"""P0-06: probe_thresholds.json を本番の 3 判定へ配線する (値は不変)。

不変条件: 現行の割当で表から解決した値が、従来の定数と完全に一致すること。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.digest.pir_daily_focus import PIR_FOCUS_CLAIM_THRESHOLD
from src.eventnews.org_identity import EVENT_MIN_LOGIT, ORG_MIN_LOGIT
from src.pipeline.briefing import THIN_BODY_PROBE_THRESHOLD
from src.tools import probe_thresholds as pt
from src.tools.model_tiers import Step

#: 現行の割当 (モデル割当 API の GET で確認した実効モデル)。
CURRENT_ASSIGNMENT: dict[Step, str] = {
    Step.THIN_BODY_JUDGE: "kuebiko-sft:s24ci65",
    Step.PIR_FOCUS_CLAIM_JUDGE: "kuebiko-sft:s23i",
    Step.ORG_IDENTITY_JUDGE: "gemma4:26b",
    Step.ASSIGN_EVIDENCE_JUDGE: "gemma4:26b",
}

#: 従来の定数 (配線前に各モジュールが直接使っていた値)。
LEGACY_CONSTANTS: list[tuple[Step, str, float]] = [
    (Step.THIN_BODY_JUDGE, "main", THIN_BODY_PROBE_THRESHOLD),
    (Step.PIR_FOCUS_CLAIM_JUDGE, "main", PIR_FOCUS_CLAIM_THRESHOLD),
    (Step.ORG_IDENTITY_JUDGE, "event_logit", EVENT_MIN_LOGIT),
    (Step.ORG_IDENTITY_JUDGE, "org_logit", ORG_MIN_LOGIT),
]


def _assign(monkeypatch: pytest.MonkeyPatch, mapping: dict[Step, str]) -> None:
    monkeypatch.setattr(pt, "resolve_step_model", lambda step, **_: mapping[step])


@pytest.mark.parametrize(("step", "question", "constant"), LEGACY_CONSTANTS)
def test_resolved_value_equals_legacy_constant_for_current_assignment(
    monkeypatch: pytest.MonkeyPatch, step: Step, question: str, constant: float
) -> None:
    _assign(monkeypatch, CURRENT_ASSIGNMENT)

    resolved = pt.effective_threshold(step, question, fallback=-999.0)

    assert resolved == constant


@pytest.mark.parametrize(("step", "question", "constant"), LEGACY_CONSTANTS)
def test_flag_off_returns_the_fallback_constant(
    monkeypatch: pytest.MonkeyPatch, step: Step, question: str, constant: float
) -> None:
    monkeypatch.setenv(pt.PROBE_THRESHOLDS_TABLE_ENV, "0")
    monkeypatch.setattr(pt, "resolve_step_model", lambda *_, **__: pytest.fail("解決しない"))

    assert pt.effective_threshold(step, question, fallback=constant) == constant


def test_model_without_entry_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _assign(monkeypatch, {**CURRENT_ASSIGNMENT, Step.THIN_BODY_JUDGE: "kuebiko-sft:s99"})

    assert pt.effective_threshold(Step.THIN_BODY_JUDGE, fallback=0.075) is None


def test_resolve_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(step: Step, **_: object) -> str:
        raise RuntimeError("db down")

    monkeypatch.setattr(pt, "resolve_step_model", boom)

    assert pt.effective_threshold(Step.THIN_BODY_JUDGE, fallback=0.075) is None


def test_underscore_keys_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text(
        json.dumps(
            {
                "m": {
                    "thin_body_judge": {"main": 0.1, "_calibration": {"kind": "isotonic"}},
                    "_meta": {"main": 0.9},
                },
                "_note": {"thin_body_judge": {"main": 0.5}},
            }
        ),
        encoding="utf-8",
    )

    assert pt.load_thresholds(path) == {"m": {"thin_body_judge": {"main": 0.1}}}


def test_explicit_model_is_used_without_resolving(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pt, "resolve_step_model", lambda *_, **__: pytest.fail("解決しない"))

    value = pt.effective_threshold(
        Step.ORG_IDENTITY_JUDGE, "org_logit", fallback=0.0, model="gemma4:26b"
    )

    assert value == ORG_MIN_LOGIT


def test_real_table_has_current_thin_body_entry() -> None:
    """本物の JSON: 版 44 の割当 (薄い本文) の項があり、他の項は変わっていない。"""
    table = pt.load_thresholds()

    assert table["kuebiko-sft:s24ci65"]["thin_body_judge"] == {"main": 0.075}
    assert table["kuebiko-sft:s23i"]["pir_focus_claim_judge"] == {"main": 0.22}
    assert table["gemma4:26b"]["org_identity_judge"] == {"event_logit": 12.0, "org_logit": -4.0}


@pytest.mark.asyncio
async def test_org_margin_unchanged_and_missing_entry_means_no_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config_loader import AppConfig
    from src.eventnews import org_identity as oi

    async def fake_probe(*_: object, **__: object) -> float:
        return 14.0

    monkeypatch.setattr(oi, "_probe_logit", fake_probe)
    monkeypatch.setattr(oi, "resolve_step_model", lambda step, db_path=None: "gemma4:26b")

    margin = await oi.judge_name_pair("A", "a", "B", "b", config=AppConfig())
    assert margin == min(14.0 - EVENT_MIN_LOGIT, 14.0 - ORG_MIN_LOGIT)

    monkeypatch.setattr(oi, "resolve_step_model", lambda step, db_path=None: "gemma4:unlisted")
    assert await oi.judge_name_pair("A", "a", "B", "b", config=AppConfig()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected"),
    [("kuebiko-sft:s24ci65", ["a"]), ("kuebiko-sft:unlisted", [])],
)
async def test_thin_body_probe_uses_table(
    monkeypatch: pytest.MonkeyPatch, model: str, expected: list[str]
) -> None:
    from src.config_loader import AppConfig
    from src.pipeline import briefing

    async def fake_choice(*_: object, **__: object) -> dict[str, float]:
        return {"Yes": 0.05, "No": 0.95}  # 0.075 未満 = 中身なし

    monkeypatch.setenv("THIN_BODY_PROBE", "1")
    monkeypatch.setattr(briefing, "probe_choice", fake_choice)
    monkeypatch.setattr(pt, "resolve_step_model", lambda step, **_: model)

    reason = await briefing._thin_body_probe_reason(
        "短い本文", title="t", article_id="x1", config=AppConfig()
    )

    assert (reason == "thin_body_probe") == bool(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "failed"), [("kuebiko-sft:s23i", 1), ("kuebiko-sft:unlisted", 0)]
)
async def test_pir_claim_gate_uses_table(
    monkeypatch: pytest.MonkeyPatch, model: str, failed: int
) -> None:
    from src.config_loader import AppConfig
    from src.digest import pir_daily_focus as pdf

    async def low_score(*_: object, **__: object) -> float:
        return 0.1  # 0.22 未満

    monkeypatch.setattr(pdf, "_claim_max_score", low_score)
    monkeypatch.setattr(pt, "resolve_step_model", lambda step, **_: model)

    _, failed_sentences = await pdf._score_claim_sentences(
        "これは検証用の一文です。", [], config=AppConfig()
    )

    assert len(failed_sentences) == failed
