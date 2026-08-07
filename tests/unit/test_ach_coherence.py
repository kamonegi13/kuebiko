"""ACH 判定整合の不変量テスト (2026-07-25 ACH 整合監査)。

leading スカラとマトリクス verdict の二重表現は、旧 apply_adversarial の片方更新で
自己矛盾 revision を生んだ (388 中 21 件、ANCHOR-CI 事案 rev13)。ここでは:
- verdict 導出の SSoT (derive_verdict / reconcile_hypotheses)
- adversarial flip の規律 (反証済み仮説への flip 見送り + flip 時のマトリクス再導出)
- 台帳書込境界 (_revision_from_judgment) の最終ガード
- backfill プランナ (plan_coherence_fix) の 3 分岐
を固定する。
"""

from __future__ import annotations

import json
from typing import Any

from src.assessment.coherence import plan_coherence_fix
from src.assessment.ledger import _revision_from_judgment
from src.synthesis.grounded.adversary import AdversarialResult, apply_adversarial
from src.synthesis.grounded.estimate import (
    HypothesisScore,
    KeyJudgment,
    derive_verdict,
    is_counts_refuted,
    reconcile_hypotheses,
)


def _h(name: str, c: int, i: int, verdict: str) -> HypothesisScore:
    return HypothesisScore(hypothesis=name, consistent=c, inconsistent=i, verdict=verdict)  # type: ignore[arg-type]


def _judgment(
    leading: str, hypotheses: tuple[HypothesisScore, ...], confidence: str = "moderate"
) -> KeyJudgment:
    return KeyJudgment(
        id="s-test",
        claim="テスト claim",
        domain="cyber_incident",
        leading_hypothesis=leading,
        confidence=confidence,  # type: ignore[arg-type]
        confidence_basis="ACH=moderate / source_basis=high",
        hypotheses=hypotheses,
        evidence=(),
    )


class TestDeriveVerdict:
    def test_leading_hypothesis_gets_leading(self) -> None:
        assert (
            derive_verdict(hypothesis="a", consistent=0, inconsistent=5, leading="a") == "leading"
        )

    def test_zero_zero_is_unscored(self) -> None:
        assert (
            derive_verdict(hypothesis="b", consistent=0, inconsistent=0, leading="a") == "unscored"
        )

    def test_more_inconsistent_is_refuted(self) -> None:
        assert (
            derive_verdict(hypothesis="b", consistent=1, inconsistent=2, leading="a") == "refuted"
        )

    def test_otherwise_viable(self) -> None:
        assert derive_verdict(hypothesis="b", consistent=2, inconsistent=2, leading="a") == "viable"


class TestReconcileHypotheses:
    def test_fixes_stale_verdicts(self) -> None:
        # 旧 leading (a) の verdict が leading のまま、新 leading (b) が refuted のまま —
        # ANCHOR-CI rev13 と同型の自己矛盾を導出で解消する
        hyps = (_h("a", 9, 0, "leading"), _h("b", 0, 1, "refuted"))
        out = reconcile_hypotheses(hyps, "b")
        by_name = {h.hypothesis: h.verdict for h in out}
        assert by_name["b"] == "leading"
        assert by_name["a"] == "viable"  # 9/0 は反証条件を満たさない

    def test_appends_missing_leading_as_unscored_counts(self) -> None:
        hyps = (_h("a", 3, 0, "leading"),)
        out = reconcile_hypotheses(hyps, "c")
        assert [h.hypothesis for h in out] == ["a", "c"]
        added = out[-1]
        assert (added.consistent, added.inconsistent, added.verdict) == (0, 0, "leading")

    def test_noop_when_coherent(self) -> None:
        hyps = (_h("a", 3, 0, "leading"), _h("b", 0, 2, "refuted"))
        assert reconcile_hypotheses(hyps, "a") == hyps


class TestApplyAdversarial:
    def test_not_refuted_records_note_only(self) -> None:
        j = _judgment("a", (_h("a", 3, 0, "leading"),))
        r = AdversarialResult(judgment_id="s-test", refutes_leading=False, strongest_counter="x")
        out = apply_adversarial(j, r)
        assert out.leading_hypothesis == "a"
        assert out.adversarial_refuted is False
        assert out.adversarial_note == "x"

    def test_flip_to_viable_reconciles_matrix(self) -> None:
        j = _judgment("a", (_h("a", 3, 0, "leading"), _h("b", 3, 1, "viable")))
        r = AdversarialResult(
            judgment_id="s-test",
            refutes_leading=True,
            strongest_counter="counter",
            recommended_leading="b",
        )
        out = apply_adversarial(j, r)
        assert out.leading_hypothesis == "b"
        assert out.confidence == "low"
        by_name = {h.hypothesis: h.verdict for h in out.hypotheses}
        assert by_name["b"] == "leading"
        assert by_name["a"] == "viable"

    def test_flip_to_counts_refuted_is_suppressed(self) -> None:
        # ANCHOR-CI rev13 の再現条件: 証拠採点で反証済み (0/1) の仮説を推奨されても
        # 主説は維持し、効果は確度降格 + 記録に留める
        j = _judgment("a", (_h("a", 9, 0, "leading"), _h("b", 0, 1, "refuted")))
        r = AdversarialResult(
            judgment_id="s-test",
            refutes_leading=True,
            strongest_counter="counter",
            recommended_leading="b",
        )
        out = apply_adversarial(j, r)
        assert out.leading_hypothesis == "a"
        assert out.confidence == "low"
        assert out.adversarial_refuted is True
        by_name = {h.hypothesis: h.verdict for h in out.hypotheses}
        assert by_name["a"] == "leading"
        assert by_name["b"] == "refuted"

    def test_unknown_recommendation_keeps_leading(self) -> None:
        j = _judgment("a", (_h("a", 3, 0, "leading"),))
        r = AdversarialResult(
            judgment_id="s-test",
            refutes_leading=True,
            strongest_counter="counter",
            recommended_leading="nonexistent",
        )
        out = apply_adversarial(j, r)
        assert out.leading_hypothesis == "a"
        assert out.confidence == "low"


class TestRevisionWriteGuard:
    def test_incoherent_judgment_is_reconciled_at_write(self) -> None:
        # 上流の是正漏れを想定した自己矛盾 judgment — 書込境界で必ず整合になる
        j = _judgment("b", (_h("a", 9, 0, "leading"), _h("b", 0, 1, "refuted")))
        row = _revision_from_judgment(
            j,
            situation_id="s-test",
            delta_type="no_change",
            delta_note="",
            now_iso="2026-07-25T00:00:00+00:00",
        )
        stored = {h["hypothesis"]: h["verdict"] for h in json.loads(row.hypotheses_json)}
        assert row.leading_hypothesis == "b"
        assert stored["b"] == "leading"
        assert stored["a"] == "viable"


class TestPlanCoherenceFix:
    def test_coherent_revision_needs_nothing(self) -> None:
        hyps: list[dict[str, Any]] = [
            {"hypothesis": "a", "consistent": 3, "inconsistent": 0, "verdict": "leading"},
            {"hypothesis": "b", "consistent": 0, "inconsistent": 2, "verdict": "refuted"},
        ]
        assert plan_coherence_fix("a", hyps).action == "none"

    def test_reverts_flip_to_counts_refuted_hypothesis(self) -> None:
        # ANCHOR-CI rev13 実データ形: 列=reporting_artifact (0/1) / マトリクス主説=strategic (9/0)
        hyps: list[dict[str, Any]] = [
            {
                "hypothesis": "strategic_coercion",
                "consistent": 9,
                "inconsistent": 0,
                "verdict": "leading",
            },
            {
                "hypothesis": "reporting_artifact",
                "consistent": 0,
                "inconsistent": 1,
                "verdict": "refuted",
            },
        ]
        plan = plan_coherence_fix("reporting_artifact", hyps)
        assert plan.action == "revert_flip"
        assert plan.new_leading == "strategic_coercion"
        verdicts = {h["hypothesis"]: h["verdict"] for h in plan.hypotheses}
        assert verdicts["strategic_coercion"] == "leading"
        assert verdicts["reporting_artifact"] == "refuted"

    def test_reconciles_when_scalar_is_healthy(self) -> None:
        # スカラは健在 (3/1) だが verdict ラベルが旧 leading のまま → スカラ尊重で導出し直し
        hyps: list[dict[str, Any]] = [
            {"hypothesis": "a", "consistent": 3, "inconsistent": 0, "verdict": "leading"},
            {"hypothesis": "b", "consistent": 3, "inconsistent": 1, "verdict": "viable"},
        ]
        plan = plan_coherence_fix("b", hyps)
        assert plan.action == "reconcile"
        assert plan.new_leading == "b"
        verdicts = {h["hypothesis"]: h["verdict"] for h in plan.hypotheses}
        assert verdicts["b"] == "leading"
        assert verdicts["a"] == "viable"

    def test_appends_missing_scalar_row(self) -> None:
        hyps: list[dict[str, Any]] = [
            {"hypothesis": "a", "consistent": 2, "inconsistent": 0, "verdict": "leading"},
        ]
        plan = plan_coherence_fix("c", hyps)
        assert plan.action == "reconcile"
        names = [h["hypothesis"] for h in plan.hypotheses]
        assert names == ["a", "c"]
        assert plan.hypotheses[-1]["verdict"] == "leading"


def test_is_counts_refuted() -> None:
    assert is_counts_refuted(_h("x", 0, 1, "refuted")) is True
    assert is_counts_refuted(_h("x", 1, 1, "viable")) is False
