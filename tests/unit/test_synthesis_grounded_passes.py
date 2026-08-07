"""Grounded synthesis の LLM パス (nominate / ground+ACH) のマッピング・正規化テスト。

FakeLLM で generate_structured を差し替え、DB なしでパスのコードロジック (tier 付与・未知値正規化・
未知仮説 drop・USB 型の確度 cap) を検証する。設計: docs/synthesis_reliability_redesign.md。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.synthesis.grounded.estimate import final_confidence
from src.synthesis.grounded.passes import (
    _WireAnalysis,
    _WireEvidence,
    _WireHypothesis,
    _WireNomination,
    _WireNominationResult,
    ground_and_score,
    nominate_claims,
)


class FakeLLM:
    """generate_structured を schema で分岐して返す test double (LLM 呼出なし)。"""

    def __init__(
        self, nomination: _WireNominationResult | None = None, analysis: _WireAnalysis | None = None
    ) -> None:
        self._nom = nomination
        self._ana = analysis
        self.model = "fake"

    async def generate(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def generate_structured(self, prompt: str, schema: type, **kw: Any) -> Any:
        if schema is _WireNominationResult:
            return self._nom
        if schema is _WireAnalysis:
            return self._ana
        raise AssertionError(f"unexpected schema {schema}")


@pytest.mark.asyncio
async def test_nominate_maps_and_filters() -> None:
    nom = _WireNominationResult(
        claims=[
            _WireNomination(claim="USB混入", domain="cyber_incident", article_ids=["a1", "a2"]),
            _WireNomination(claim="", domain="x", article_ids=["a3"]),  # 空 claim → drop
            _WireNomination(claim="no ids", domain="x", article_ids=[]),  # ids 空 → drop
        ]
    )
    out = await nominate_claims(
        llm=FakeLLM(nomination=nom),  # type: ignore[arg-type]
        articles=[{"article_id": "a1", "title": "t", "feed_title": "f"}],
        period_label="L",
        k=5,
    )
    assert len(out) == 1
    assert out[0].claim == "USB混入"
    assert out[0].article_ids == ("a1", "a2")


@pytest.mark.asyncio
async def test_ground_injects_tier_and_normalizes() -> None:
    ana = _WireAnalysis(
        evidence=[
            _WireEvidence(
                article_id="a1",
                attribution_basis="tooling_similarity",
                excerpt="同種",
                polarity="supports",
            ),
            _WireEvidence(
                article_id="a1",
                attribution_basis="BOGUS",
                excerpt="窃取証拠なし",
                polarity="contradicts",
            ),  # 未知 basis → unattributed
            _WireEvidence(
                article_id="a1",
                attribution_basis="govt_confirmed",
                excerpt="  ",
                polarity="neutral",
            ),  # 空 excerpt → drop
        ],
        hypotheses=[
            _WireHypothesis(hypothesis="organized_state_op", consistent=1, inconsistent=3),
            _WireHypothesis(hypothesis="opportunistic_commodity", consistent=3, inconsistent=0),
            _WireHypothesis(hypothesis="BOGUS_HYP", consistent=5, inconsistent=0),  # 未知 → drop
        ],
        leading_hypothesis="opportunistic_commodity",
        confidence="moderate",
        key_assumptions=["a"],
        missing_evidence=["窃取証拠"],
        indicators=["同種事案"],
    )
    res = await ground_and_score(
        llm=FakeLLM(analysis=ana),  # type: ignore[arg-type]
        claim="USB",
        domain="cyber_incident",
        sources=[{"article_id": "a1", "feed_title": "NHK", "text": "..."}],
        tier_by_id={"a1": "news"},
    )
    assert len(res.evidence) == 2  # 空 excerpt は drop
    assert res.evidence[0].source_tier == "news"  # tier はコードが付与 (LLM 不信)
    assert res.evidence[1].attribution_basis == "unattributed"  # 未知 basis 正規化
    assert {h.hypothesis for h in res.hypotheses} == {
        "organized_state_op",
        "opportunistic_commodity",
    }  # 未知 hypothesis は drop
    assert res.leading_hypothesis == "opportunistic_commodity"
    assert res.llm_confidence == "moderate"


@pytest.mark.asyncio
async def test_verdict_unscored_for_zero_counts() -> None:
    # 0/0 の仮説は viable でなく unscored (LLM が実質評価しなかったものを格上げしない)
    ana = _WireAnalysis(
        evidence=[_WireEvidence(article_id="a1", excerpt="x", polarity="neutral")],
        hypotheses=[
            _WireHypothesis(hypothesis="opportunistic_commodity", consistent=2, inconsistent=0),
            _WireHypothesis(hypothesis="hacktivism_influence", consistent=0, inconsistent=0),
        ],
        leading_hypothesis="opportunistic_commodity",
        confidence="moderate",
    )
    res = await ground_and_score(
        llm=FakeLLM(analysis=ana),  # type: ignore[arg-type]
        claim="c",
        domain="cyber_incident",
        sources=[{"article_id": "a1", "feed_title": "X", "text": "..."}],
        tier_by_id={"a1": "news"},
    )
    by_id = {h.hypothesis: h for h in res.hypotheses}
    assert by_id["hacktivism_influence"].verdict == "unscored"
    assert by_id["opportunistic_commodity"].verdict == "leading"


@pytest.mark.asyncio
async def test_unknown_leading_falls_back_to_unverified() -> None:
    ana = _WireAnalysis(
        evidence=[_WireEvidence(article_id="a1", excerpt="x")],
        hypotheses=[],
        leading_hypothesis="",  # parse 欠落
        confidence="low",
    )
    res = await ground_and_score(
        llm=FakeLLM(analysis=ana),  # type: ignore[arg-type]
        claim="c",
        domain="cyber_incident",
        sources=[{"article_id": "a1", "feed_title": "X", "text": "..."}],
        tier_by_id={"a1": "news"},
    )
    assert res.leading_hypothesis == "unverified_or_false"


@pytest.mark.asyncio
async def test_usb_golden_organized_capped_but_not_banned() -> None:
    # USB 型: 仮に LLM が organized を leading + high にしても、帰属が tooling 類似のみ →
    # 確度は low に抑制 (過確信防止)。ただし leading は変えない (組織的仮説を禁止しない = 対称)。
    ana = _WireAnalysis(
        evidence=[
            _WireEvidence(
                article_id="a1",
                attribution_basis="tooling_similarity",
                excerpt="同種ツール",
                polarity="supports",
            )
        ],
        hypotheses=[_WireHypothesis(hypothesis="organized_state_op", consistent=1, inconsistent=2)],
        leading_hypothesis="organized_state_op",
        confidence="high",
    )
    res = await ground_and_score(
        llm=FakeLLM(analysis=ana),  # type: ignore[arg-type]
        claim="USB",
        domain="cyber_incident",
        sources=[{"article_id": "a1", "feed_title": "X", "text": "..."}],
        tier_by_id={"a1": "news"},
    )
    conf, reason = final_confidence(
        res.llm_confidence, "high", res.leading_hypothesis, res.evidence
    )
    assert conf == "low"  # 強い source でも帰属弱 → low
    assert "帰属" in reason
    assert res.leading_hypothesis == "organized_state_op"  # 禁止はしない
