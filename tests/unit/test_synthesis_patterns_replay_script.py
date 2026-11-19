"""scripts/synthesis_patterns_replay.py の純粋部分 (計数の要約・estimate の取り出し)。"""

from __future__ import annotations

import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from src.storage.records import StatusSynthesisRecord
from src.synthesis.grounded.estimate import estimate_to_dict
from tests.unit.n20_t5_fixtures import BASE_TS, estimate

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "synthesis_patterns_replay.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("synthesis_patterns_replay", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(tradecraft: str) -> StatusSynthesisRecord:
    return StatusSynthesisRecord(
        period_type="daily",
        period_start=BASE_TS,
        period_end=BASE_TS + timedelta(days=1),
        headline="h",
        weight_section="w",
        chain_section="c",
        cog_section="g",
        spillover_section="s",
        pir_section="p",
        axes_evidence="{}",
        tradecraft=tradecraft,
    )


def test_estimate_of_reads_grounded_estimate(script: ModuleType) -> None:
    est = estimate()
    rec = _record(json.dumps({"grounded_estimate": estimate_to_dict(est)}))
    assert script.estimate_of(rec) == est


@pytest.mark.parametrize("tradecraft", ["", "not json", "{}", '{"grounded_estimate": 3}'])
def test_estimate_of_returns_none_for_missing_or_broken(
    script: ModuleType, tradecraft: str
) -> None:
    try:
        assert script.estimate_of(_record(tradecraft)) is None
    except json.JSONDecodeError:
        pytest.fail("broken JSON must not raise")


def test_summarize_counts_days_with_and_without_patterns(script: ModuleType) -> None:
    rows = [{"has_pattern": True, "patterns": 2}, {"has_pattern": False, "patterns": 0}]
    assert script.summarize(rows) == {
        "days": 2,
        "days_with_pattern": 1,
        "days_without_pattern": 1,
        "patterns_per_day_mean": 1.0,
        "patterns_per_day_max": 2,
    }
    assert script.summarize([])["days"] == 0


def test_judge_input_lists_claims_and_up_to_three_excerpts(script: ModuleType) -> None:
    from dataclasses import replace

    from src.synthesis.grounded.estimate import EvidenceItem

    est = estimate()
    first = est.judgments[0]
    ev = tuple(EvidenceItem(f"a{n}", "t", "vendor_confirmed", f"抜粋{n}") for n in range(5))
    est = replace(est, judgments=(replace(first, evidence=ev), *est.judgments[1:]))
    text = script.judge_input(est)
    assert f"[{first.id}] {first.claim}" in text
    assert "  - 根拠の抜粋: 抜粋2" in text and "抜粋3" not in text


def test_count_row_reports_titles_subjects_and_marks(script: ModuleType) -> None:
    from src.storage.repo_judgment_attrs import EventHeadline
    from src.synthesis.grounded.pattern_render import make_plan
    from tests.unit.synthesis_pattern_fixtures import BREACH, attrs, j

    est = estimate()
    js = (j("s-1", "ゼータ運輸で侵害。"), j("s-2", "乙社で侵害。"))
    est = replace_judgments(est, js)
    head = (EventHeadline("ev-1", 1, "2026-11-17T00:00:00", "ゼータ運輸の不正アクセス"),)
    at = {"s-1": attrs(kinds=BREACH, headlines=head), "s-2": attrs(kinds=BREACH)}
    plan = make_plan(est, at)
    row = script.count_row(_record(""), est, plan)
    assert {t["title"]: t["from_event"] for t in row["titles"]} == {
        "ゼータ運輸の不正アクセス": True,
        "乙社で侵害": False,
    }
    assert row["headline_subjects"][0]["title"] in {"ゼータ運輸の不正アクセス", "乙社で侵害"}
    assert row["common_marks"] == {"P1": []}


def replace_judgments(est: Any, js: Any) -> Any:
    from dataclasses import replace

    return replace(est, judgments=js)
