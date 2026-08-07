"""段4 narrative 射影 render のテスト。tradecraft は決定論投影、sections は制約付き LLM。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from src.synthesis.grounded.estimate import Estimate, EvidenceItem, HypothesisScore, KeyJudgment
from src.synthesis.grounded.render import (
    _WireSections,
    project_tradecraft,
    render_record,
    render_sections,
)


def _est() -> Estimate:
    j1 = KeyJudgment(
        id="j1",
        claim="自衛隊網へのUSBマルウェア混入",
        domain="cyber_incident",
        leading_hypothesis="opportunistic_commodity",
        confidence="low",
        confidence_basis="ACH=low / source_basis=medium",
        hypotheses=(
            HypothesisScore("opportunistic_commodity", 3, 0, "leading"),
            HypothesisScore("organized_state_op", 1, 2, "viable"),
            HypothesisScore("hacktivism_influence", 0, 0, "unscored"),
        ),
        evidence=(
            EvidenceItem("a1", "victim_disclosed", "tooling_similarity", "偽造USB", "supports"),
        ),
        key_assumptions=("偽造品が広く流通",),
        missing_evidence=("窃取・C2通信の証拠",),
        indicators=("同種事案の多発",),
        adversarial_refuted=True,
        adversarial_note="標的選定の証拠があれば組織的作戦の余地",
    )
    return Estimate(
        period_type="daily",
        period_start=datetime(2026, 6, 29, tzinfo=UTC),
        period_end=datetime(2026, 6, 29, 12, tzinfo=UTC),
        judgments=(j1,),
        model="gemma4:31b",
    )


class TestProjectTradecraft:
    def test_leading_and_alternatives_and_missing(self) -> None:
        tc = project_tradecraft(_est())
        assert "USBマルウェア混入" in tc["leading_assessment"]
        assert "低確度" in tc["leading_assessment"]
        # 対立仮説 (viable) + 検証反証が alternatives に出る
        joined = " ".join(tc["alternatives"])
        assert "組織的国家作戦" in joined  # viable な対立
        assert "標的選定" in joined  # adversarial note
        assert "窃取・C2通信の証拠" in tc["missing_evidence"]
        assert tc["source_caveat"]

    def test_empty_estimate(self) -> None:
        est = Estimate(
            "daily", datetime(2026, 6, 29, tzinfo=UTC), datetime(2026, 6, 29, tzinfo=UTC), ()
        )
        tc = project_tradecraft(est)
        assert tc["leading_assessment"] == ""
        assert tc["alternatives"] == []


class _FakeLLM:
    model = "fake"

    async def generate(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def generate_structured(self, prompt: str, schema: type, **kw: Any) -> Any:
        return _WireSections(
            headline=(
                "自衛隊網で確認された偽造USB経由のマルウェア混入は、日和見的なコモディティ"
                "汚染の可能性があるが確証はない (低確度)。標的型の証拠は未確認で、"
                "流通経路の監視が焦点となる。"
            ),
            weight_section="w",
            chain_section="c",
            cog_section="cog",
            spillover_section="s",
            pir_section="p",
        )


@pytest.mark.asyncio
async def test_render_sections_empty_judgments_skips_llm() -> None:
    est = Estimate(
        "daily", datetime(2026, 6, 29, tzinfo=UTC), datetime(2026, 6, 29, tzinfo=UTC), ()
    )
    sec = await render_sections(llm=_FakeLLM(), est=est, period_label="L")  # type: ignore[arg-type]
    assert "主要判定" in sec.headline


@pytest.mark.asyncio
async def test_render_record_builds_valid_record() -> None:
    rec = await render_record(llm=_FakeLLM(), est=_est(), period_label="L", article_count=5)  # type: ignore[arg-type]
    assert rec.period_type == "daily"
    assert "偽造USB" in rec.headline
    assert rec.article_count == 5
    # tradecraft は parse 可能な JSON、leading_assessment を含む
    tc = json.loads(rec.tradecraft)
    assert "leading_assessment" in tc
    assert "低確度" in tc["leading_assessment"]


def test_project_tradecraft_merges_forecast_ctx() -> None:
    """監査 2026-07-05 P4: forecast lifecycle の tradecraft 射影 (空ハードコードの解消)。"""
    ctx = {
        "forecasts": [
            {"claim": "[X] 新たな被害公表", "horizon": "next_month", "confidence": "medium"}
        ],
        "forecast_scorecard": [{"claim": "[X] 指標Y", "verdict": "realized", "reason": "発火"}],
        "forecast_alignment": "本期間に監視指標 1 件が発火 (予測的中)。",
        "freshness_note": "追跡中の全 3 情勢に直近 7 日以内の裏付けあり。",
    }
    tc = project_tradecraft(_est(), forecast_ctx=ctx)
    assert tc["forecasts"][0]["horizon"] == "next_month"
    assert tc["forecast_scorecard"][0]["verdict"] == "realized"
    assert "発火" in tc["forecast_alignment"]
    assert tc["freshness_note"]


def test_project_tradecraft_without_ctx_keeps_empty() -> None:
    tc = project_tradecraft(_est())
    assert tc["forecasts"] == []
    assert tc["forecast_alignment"] == ""
