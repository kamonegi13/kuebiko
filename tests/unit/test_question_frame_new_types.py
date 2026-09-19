"""問いの型 X (エスカレーション軌道) / P (波及) — 2026-09-19 追加。

積み重ねの追跡 (単一事象ではなく、事象が足されて趨勢が動く) の受け皿。
docs/tracking_unit_separation_design.md
"""

from __future__ import annotations

import pytest

from src.assessment.question_frame import (
    ESCALATION_STAGES,
    FRAME_BY_ID,
    PROPAGATION_KINDS,
    aggregate_population,
    render_question,
)


def test_escalation_renders_with_two_parties_and_a_stage() -> None:
    q = render_question(
        FRAME_BY_ID["escalation"], {"subject": "us", "counterpart": "ir", "stage": "military"}
    )
    assert q == "米国とイランの対立は直接的な武力行使の段階へ進むか"


def test_propagation_renders_origin_policy_and_target() -> None:
    q = render_question(
        FRAME_BY_ID["propagation"],
        {
            "origin": "cn",
            "policy": "infrastructure_influence",
            "target_country": "JP",
            "target_scope": "critical_infrastructure",
        },
    )
    assert q == "中国のインフラ支配の拡大は日本の重要インフラへ波及するか"


def test_missing_slot_raises_rather_than_making_a_holed_question() -> None:
    with pytest.raises(KeyError):
        render_question(FRAME_BY_ID["escalation"], {"subject": "us", "counterpart": "ir"})


def test_new_frames_have_competing_hypotheses_including_a_null() -> None:
    for fid, null_id in (
        ("escalation", "escalation_contained"),
        ("propagation", "propagation_not_reached"),
    ):
        ids = [h.id for h in FRAME_BY_ID[fid].hypotheses]
        assert null_id in ids, f"{fid} に帰無仮説が無い"
        assert len(ids) >= 3, f"{fid} の対立仮説が少なすぎる"


def test_new_frames_do_not_supply_aggregate_signal() -> None:
    # 段階の進行・波及は件数でも構成比でも測れない (質的な新手段の出現で判断する)
    assert aggregate_population("escalation", {"subject": "us", "counterpart": "ir"}) == {}
    assert aggregate_population("propagation", {"origin": "cn", "target_country": "JP"}) == {}


def test_slot_vocabularies_are_defined_here_as_ssot() -> None:
    assert "military" in ESCALATION_STAGES and "economic" in ESCALATION_STAGES
    assert "export_control" in PROPAGATION_KINDS and "sanction" in PROPAGATION_KINDS
