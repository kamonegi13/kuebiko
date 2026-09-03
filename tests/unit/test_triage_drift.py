"""triage ドリフト検知 (src/eval/triage_drift.py) の不変条件。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from src.eval import triage_drift
from src.tools.article_triage import TriageDecision
from src.tools.llm_client import LLMClient


class _Llm:
    model = "test"

    def __init__(self, answers: list[str]):
        self._answers = list(answers)

    async def generate_structured(self, *a, **k):  # type: ignore[no-untyped-def]
        return TriageDecision(importance=self._answers.pop(0))  # type: ignore[arg-type]


def _goldset(tmp_path: Path) -> Path:
    p = tmp_path / "goldset.json"
    rows = [
        {"prompt": "p1", "now_26b": "high"},
        {"prompt": "p2", "now_26b": "medium"},
        {"prompt": "p3", "now_26b": "low"},
    ]
    p.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_no_movement_means_zero_drift(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Arrange — day-0 と同じ答えを返す
    monkeypatch.setattr(
        triage_drift,
        "build_llm_for",
        lambda *a, **k: cast(LLMClient, _Llm(["high", "medium", "low"])),
    )
    monkeypatch.setattr(triage_drift, "load_app_config", lambda: None)

    # Act
    got = await triage_drift.measure_drift(_goldset(tmp_path))

    # Assert
    assert got is not None and got.moved == 0 and got.rate == 0.0


@pytest.mark.asyncio
async def test_demotion_toward_low_is_counted_separately(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """見逃し方向 (high/medium → low) は別枠で数える — 任務上いちばん危険な移動。"""
    # Arrange — high→low / medium→high / low→low
    monkeypatch.setattr(
        triage_drift, "build_llm_for", lambda *a, **k: cast(LLMClient, _Llm(["low", "high", "low"]))
    )
    monkeypatch.setattr(triage_drift, "load_app_config", lambda: None)

    # Act
    got = await triage_drift.measure_drift(_goldset(tmp_path))

    # Assert
    assert got is not None
    assert got.moved == 2
    assert got.demoted == 1  # high→low だけ
    assert got.rate == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_missing_goldset_returns_none(tmp_path: Path) -> None:
    assert await triage_drift.measure_drift(tmp_path / "nai.json") is None
