"""Grounded synthesis 基盤層 (確度 cap + 過剰帰属ガードレール) のテスト。

LLM を呼ばない純粋層の検証。USB 事案型の過剰帰属が構造的に止まることを担保する。
詳細設計: docs/synthesis_reliability_redesign.md。
"""

from __future__ import annotations

from src.synthesis.grounded.estimate import (
    AttributionBasis,
    EvidenceItem,
    Polarity,
    attribution_confidence_cap,
    cap_confidence,
    evidence_ceiling,
    final_confidence,
    has_strong_attribution,
)
from src.synthesis.grounded.hypotheses import (
    CYBER_HYPOTHESES,
    GEO_HYPOTHESES,
    hypotheses_for_domain,
    hypothesis_ids,
    is_known_hypothesis,
)


def _ev(
    basis: AttributionBasis, polarity: Polarity = "supports", tier: str = "news"
) -> EvidenceItem:
    return EvidenceItem(
        article_id="a", source_tier=tier, attribution_basis=basis, excerpt="x", polarity=polarity
    )


class TestCapConfidence:
    def test_weak_source_caps_high_to_low(self) -> None:
        capped, reason = cap_confidence("high", "low")
        assert capped == "low"
        assert reason  # 抑制理由が付く

    def test_medium_source_caps_high_to_moderate(self) -> None:
        capped, reason = cap_confidence("high", "medium")
        assert capped == "moderate"
        assert reason

    def test_high_source_allows_high(self) -> None:
        capped, reason = cap_confidence("high", "high")
        assert capped == "high"
        assert reason == ""

    def test_low_llm_conf_unchanged_even_with_high_source(self) -> None:
        # cap は上限であって下限ではない (LLM が低いと言えば低いまま)
        capped, reason = cap_confidence("low", "high")
        assert capped == "low"
        assert reason == ""

    def test_unknown_source_conf_defaults_to_moderate_ceiling(self) -> None:
        capped, _ = cap_confidence("high", "garbage")
        assert capped == "moderate"


class TestAttributionGuardrail:
    def test_strong_attribution_present(self) -> None:
        ev = (_ev("vendor_confirmed"), _ev("tooling_similarity"))
        assert has_strong_attribution(ev) is True

    def test_only_weak_attribution(self) -> None:
        ev = (_ev("tooling_similarity"), _ev("unattributed"))
        assert has_strong_attribution(ev) is False

    def test_strong_basis_but_contradicts_does_not_count(self) -> None:
        # 強い basis でも polarity=contradicts なら organized を支持しない
        ev = (_ev("vendor_confirmed", "contradicts"),)
        assert has_strong_attribution(ev) is False

    def test_attribution_cap_low_for_organized_weak(self) -> None:
        # USB 事案型: ツール類似のみ → organized は確度 low 上限 (仮説は禁止/変更しない)
        ev = (_ev("tooling_similarity"), _ev("victim_disclosed", "contradicts"))
        assert attribution_confidence_cap("organized_state_op", ev) == "low"

    def test_attribution_cap_none_for_organized_strong(self) -> None:
        ev = (_ev("govt_confirmed"),)
        assert attribution_confidence_cap("organized_state_op", ev) is None

    def test_attribution_cap_low_for_criminal_weak(self) -> None:
        # 対称性: criminal_financial も帰属を主張する → 弱い帰属なら同じく low に cap
        ev = (_ev("claimed_by_actor"),)
        assert attribution_confidence_cap("criminal_financial", ev) == "low"

    def test_attribution_cap_low_for_hacktivism_weak(self) -> None:
        ev = (_ev("state_media_claim"),)
        assert attribution_confidence_cap("hacktivism_influence", ev) == "low"

    def test_attribution_cap_none_for_non_attributing(self) -> None:
        # commodity/accidental は「誰がやったか」を主張しない → 帰属上限の対象外
        ev = (_ev("tooling_similarity"),)
        assert attribution_confidence_cap("opportunistic_commodity", ev) is None
        assert attribution_confidence_cap("accidental_negligence", ev) is None


class TestFinalConfidence:
    """方向中立な 2 上限 (source_basis / 帰属) を合成。仮説は変えない。"""

    def test_organized_weak_attribution_capped_low(self) -> None:
        # 強い source でも、organized が tooling 類似のみなら帰属上限で low に
        ev = (_ev("tooling_similarity"),)
        conf, reason = final_confidence("high", "high", "organized_state_op", ev)
        assert conf == "low"
        assert "帰属" in reason

    def test_organized_strong_attribution_keeps_high(self) -> None:
        ev = (_ev("vendor_confirmed"),)
        conf, reason = final_confidence("high", "high", "organized_state_op", ev)
        assert conf == "high"
        assert reason == ""

    def test_benign_leading_also_capped_by_source(self) -> None:
        # 穏当側の結論も弱いソースなら同様に抑制される (対称性)
        ev = (_ev("unattributed"),)
        conf, _ = final_confidence("high", "low", "accidental_negligence", ev)
        assert conf == "low"

    def test_evidence_driven_organized_can_lead_with_strong_basis(self) -> None:
        # organized は禁止されない: 強い帰属があれば高確度で leading になり得る
        ev = (_ev("govt_confirmed"), _ev("researcher_assessed"))
        conf, reason = final_confidence("high", "high", "organized_state_op", ev)
        assert conf == "high"


class TestConsideredCount:
    """考慮した記事プール総数を estimate に保持・シリアライズ (母数の透明化)。"""

    def test_considered_count_defaults_zero_and_serializes(self) -> None:
        from datetime import UTC, datetime

        from src.synthesis.grounded.estimate import Estimate, estimate_to_dict

        est = Estimate(
            period_type="daily",
            period_start=datetime(2026, 7, 1, tzinfo=UTC),
            period_end=datetime(2026, 7, 1, tzinfo=UTC),
            judgments=(),
            considered_count=150,
        )
        d = estimate_to_dict(est)
        assert d["considered_count"] == 150
        # 既定は 0 (legacy/未設定でも壊れない)
        assert Estimate("daily", est.period_start, est.period_end, ()).considered_count == 0


class TestEvidenceCeiling:
    """強帰属が news 報道の確度上限を一段引上げる (refinement #3、方向中立)。"""

    def test_news_with_govt_attribution_steps_up_to_high(self) -> None:
        # news 媒体 (medium) が政府確定帰属を報道 → 上限は moderate でなく high
        assert evidence_ceiling("medium", (_ev("govt_confirmed"),)) == "high"

    def test_news_without_strong_attribution_stays_at_tier(self) -> None:
        # 強帰属なし → 報道 tier 由来の上限のまま (引上げない)
        assert evidence_ceiling("medium", (_ev("tooling_similarity"),)) == "moderate"

    def test_low_source_with_strong_attribution_steps_up_to_moderate(self) -> None:
        assert evidence_ceiling("low", (_ev("vendor_confirmed"),)) == "moderate"

    def test_high_source_step_up_clamped_at_high(self) -> None:
        assert evidence_ceiling("high", (_ev("govt_confirmed"),)) == "high"

    def test_final_confidence_news_govt_keeps_high(self) -> None:
        # 統合: news source_basis でも政府帰属の報道なら high を許容
        ev = (_ev("govt_confirmed"),)
        conf, reason = final_confidence("high", "medium", "organized_state_op", ev)
        assert conf == "high"
        assert reason == ""

    def test_final_confidence_news_weak_still_capped_to_moderate(self) -> None:
        # 強帰属なしの news は従来通り moderate 上限 (step-up が暴走しない)
        ev = (_ev("unattributed"),)
        conf, _ = final_confidence("high", "medium", "opportunistic_commodity", ev)
        assert conf == "moderate"


class TestHypothesisMenu:
    def test_menu_has_canonical_ids(self) -> None:
        ids = set(hypothesis_ids())
        assert {"organized_state_op", "opportunistic_commodity", "accidental_negligence"} <= ids

    def test_is_known(self) -> None:
        assert is_known_hypothesis("organized_state_op")
        assert not is_known_hypothesis("nonexistent")


class TestHypothesesForDomain:
    """ドメイン別仮説セット (refinement #1): サイバー/地政学で ACH 候補を切替える。"""

    def test_cyber_domain_returns_cyber_set(self) -> None:
        hyps = hypotheses_for_domain("cyber_incident")
        ids = {h.id for h in hyps}
        assert "organized_state_op" in ids
        assert "strategic_coercion" not in ids  # 地政学仮説は混ぜない
        assert hyps == CYBER_HYPOTHESES

    def test_geo_domain_returns_geo_set(self) -> None:
        hyps = hypotheses_for_domain("geopolitical")
        ids = {h.id for h in hyps}
        assert "strategic_coercion" in ids
        assert "organized_state_op" not in ids  # サイバー帰属仮説は混ぜない
        assert hyps == GEO_HYPOTHESES

    def test_policy_domain_uses_geo_set(self) -> None:
        # 輸出規制等の政策行動を organized_state_op に押し込めない
        ids = {h.id for h in hypotheses_for_domain("policy")}
        assert "strategic_coercion" in ids
        assert "routine_or_administrative" in ids

    def test_unknown_domain_returns_event_union_without_posture(self) -> None:
        # 段B (2026-07-13): POSTURE は常設専用 (hypotheses_override で明示指定)。
        # event の domain 選択 (不明 domain の union 含む) には漏らさない。
        ids = {h.id for h in hypotheses_for_domain("unclassified")}
        assert "organized_state_op" in ids and "strategic_coercion" in ids
        assert "posture_active_prepositioning_jp" not in ids

    def test_shared_hypotheses_in_both_sets(self) -> None:
        # reporting_artifact / unverified_or_false は両ドメインで使える
        for dom in ("cyber", "geopolitical"):
            ids = {h.id for h in hypotheses_for_domain(dom)}
            assert "reporting_artifact" in ids
            assert "unverified_or_false" in ids
