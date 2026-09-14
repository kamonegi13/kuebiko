"""``estimate_to_dict`` / ``estimate_from_dict`` の往復不変量。

凍結評価 (状況総括の審判セット) は過去窓の ``tradecraft.grounded_estimate`` を
入力に再構築する。**全腕を同じ再構築で測る**ことが前提なので、再構築がここで
壊れると審判そのものが無効になる (2026-09-08 の時代混在と同型の事故)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.synthesis.grounded.estimate import (
    Estimate,
    EvidenceItem,
    HypothesisScore,
    KeyJudgment,
    estimate_from_dict,
    estimate_to_dict,
)


def _estimate() -> Estimate:
    judgment = KeyJudgment(
        id="j1",
        claim="サンプル判定",
        domain="cyber",
        leading_hypothesis="organized_state_op",
        confidence="moderate",
        confidence_basis="vendor_confirmed",
        hypotheses=(
            HypothesisScore(
                hypothesis="organized_state_op", consistent=3, inconsistent=0, verdict="leading"
            ),
        ),
        evidence=(
            EvidenceItem(
                article_id="a1",
                source_tier="vendor",
                attribution_basis="vendor_confirmed",
                excerpt="抜粋",
                polarity="supports",
            ),
        ),
        key_assumptions=("前提",),
        missing_evidence=("欠落",),
        indicators=("指標",),
        fired_indicators=("発火",),
        pir_ids=("pir_x",),
        delta_type="escalated",
        delta_note="拡大を観測",
        implication="含意",
        japan_related=True,
        unassessed_count=2,
    )
    return Estimate(
        period_type="daily",
        period_start=datetime(2026, 9, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 2, tzinfo=UTC),
        judgments=(judgment,),
        model="test-model",
        considered_count=42,
        relations=(("j1", "j2", "same_actor", "共有アクター"),),
    )


class TestRoundTrip:
    def test_dict_roundtrip_preserves_estimate(self) -> None:
        est = _estimate()
        assert estimate_from_dict(estimate_to_dict(est)) == est

    def test_unknown_keys_are_dropped(self) -> None:
        """schema 進化で増えた/消えた欄があっても読み切る (過去窓を落とさない)。"""
        data = estimate_to_dict(_estimate())
        data["future_field"] = "未知"
        data["judgments"][0]["future_judgment_field"] = 1
        assert estimate_from_dict(data) == _estimate()

    def test_missing_optional_fields_fall_back_to_defaults(self) -> None:
        data = estimate_to_dict(_estimate())
        del data["relations"]
        del data["considered_count"]
        del data["judgments"][0]["fired_indicators"]
        rebuilt = estimate_from_dict(data)
        assert rebuilt.relations == ()
        assert rebuilt.considered_count == 0
        assert rebuilt.judgments[0].fired_indicators == ()
