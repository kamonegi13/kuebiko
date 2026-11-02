"""scripts/graphrag_eval の純関数のテスト (札の戻し・順の入れ替え・集計・Wilson・標本の抜き方)。"""

from __future__ import annotations

import pytest

from scripts.graphrag_eval.e1_rescore import claim_orders, split_insertion, tally_claims
from scripts.graphrag_eval.e2_judge import blind_inputs, present_order, to_judgement
from scripts.graphrag_eval.prompts import Claim, ClaimVerdict, EdgeVerdict
from scripts.graphrag_eval.sampling import (
    choose_sample,
    exclude_pairs,
    pair_key,
    pick_members,
    resolve_root,
    sample_pairs,
)
from scripts.graphrag_eval.stats import (
    ClaimTally,
    EdgeJudgement,
    PairwiseRow,
    arm_of_label,
    e1_task_result,
    e1_verdict,
    e2_type_result,
    e2_use,
    edge_correct,
    outputs_in_order,
    support_rate,
    unblind,
    win_rate_by_length,
)

# ------------------------------------------------------------ 札と順


def test_order_ab_maps_x_to_a_and_ba_maps_x_to_b() -> None:
    assert arm_of_label("X", "AB") == "A"
    assert arm_of_label("Y", "AB") == "B"
    assert arm_of_label("X", "BA") == "B"
    assert arm_of_label("Y", "BA") == "A"


def test_outputs_in_order_swaps_for_ba() -> None:
    assert outputs_in_order("a", "b", "AB") == ("a", "b")
    assert outputs_in_order("a", "b", "BA") == ("b", "a")


def test_unblind_keeps_tie_and_rejects_unknown() -> None:
    assert unblind("同等", "BA") == "同等"
    assert unblind("X", "BA") == "B"
    with pytest.raises(ValueError):
        unblind("Z", "AB")


# ------------------------------------------------------------ E1 の集計


def _rows(task: str, b_wins_ab: int, b_wins_ba: int) -> list[PairwiseRow]:
    rows = []
    for i in range(15):
        rows.append(PairwiseRow(task, f"k{i}", "AB", "B" if i < b_wins_ab else "A", 100, 120))
        rows.append(PairwiseRow(task, f"k{i}", "BA", "B" if i < b_wins_ba else "A", 100, 120))
    return rows


def _tallies(task: str, sup_a: int, sup_b: int) -> list[ClaimTally]:
    return [ClaimTally(task, "k0", "A", 100, sup_a, 0), ClaimTally(task, "k0", "B", 100, sup_b, 2)]


def test_e1_verdict_maintained_when_both_orders_win_and_noninferior() -> None:
    assert e1_verdict({"AB": 10, "BA": 12}, {"AB": 15, "BA": 15}, -0.03) == "維持"


def test_e1_verdict_artifact_when_one_order_at_nine() -> None:
    assert e1_verdict({"AB": 13, "BA": 9}, {"AB": 15, "BA": 15}, 0.0) == "評価の産物"


def test_e1_verdict_fails_on_support_margin() -> None:
    assert e1_verdict({"AB": 12, "BA": 12}, {"AB": 15, "BA": 15}, -0.031) == "裏付けで不合格"


def test_e1_verdict_undetermined_when_window_missing() -> None:
    assert e1_verdict({"AB": 12, "BA": 12}, {"AB": 14, "BA": 15}, 0.0) == "未確定"


def test_e1_task_result_counts_consistency_and_position() -> None:
    res = e1_task_result("spotlight", _rows("spotlight", 12, 11), _tallies("spotlight", 90, 88))
    assert res.wins_b == {"AB": 12, "BA": 11}
    assert res.n == {"AB": 15, "BA": 15}
    assert res.consistent_windows == 14  # 0-10 は両順 B、11 は AB=B・BA=A、12-14 は両順 A
    # 先に出した方 (X) の勝ち: AB で A 勝ち 3 + BA で B 勝ち 11
    assert res.position_first_wins == 14
    assert res.support_diff == pytest.approx(-0.02)
    assert res.misattributed_b == 2
    assert res.verdict == "維持"


def test_support_rate_none_without_claims() -> None:
    assert support_rate([], "A") is None


def test_win_rate_by_length_splits_by_sign() -> None:
    rows = [
        PairwiseRow("t", "k1", "AB", "B", 100, 200),
        PairwiseRow("t", "k2", "AB", "A", 300, 200),
    ]
    assert win_rate_by_length(rows) == {"b_longer": (1, 1), "b_not_longer": (0, 1)}


def test_tally_claims_counts_link_support_only_for_b() -> None:
    v = ClaimVerdict(
        claims_x=[Claim(text="x1", support="線の節のみ", misattributed=False)],
        claims_y=[
            Claim(text="y1", support="線の節のみ", misattributed=True),
            Claim(text="y2", support="裏付け無し", misattributed=False),
        ],
    )
    # 順 AB: X = A、Y = B
    out = {t.arm: t for t in tally_claims("t", "k", v, "AB")}
    assert (out["A"].total, out["A"].supported, out["A"].misattributed) == (1, 0, 0)
    assert (out["B"].total, out["B"].supported, out["B"].misattributed) == (2, 1, 1)


def test_split_insertion_returns_inserted_section() -> None:
    assert split_insertion("前半後半", "前半[線]後半") == "[線]"
    with pytest.raises(ValueError):
        split_insertion("abc", "xbc")


def test_claim_orders_deterministic() -> None:
    tasks = [("t", [{"key": "a"}, {"key": "b"}])]
    assert claim_orders(tasks) == claim_orders(tasks)
    assert set(claim_orders(tasks).values()) <= {"AB", "BA"}


# ------------------------------------------------------------ E2


def _j(types: set[str], relation: str, attacker: str, tag_a: str = "正しい") -> EdgeJudgement:
    return EdgeJudgement("a|b", frozenset(types), relation, attacker, tag_a, "正しい")


def test_edge_correct_definitions() -> None:
    assert edge_correct("same_actor", _j({"same_actor"}, "無関係", "はい"))
    assert not edge_correct("same_actor", _j({"same_actor"}, "同じアクターの別の活動", "不明"))
    assert edge_correct("incident", _j({"incident"}, "系統", "不明"))
    assert not edge_correct("lineage", _j({"lineage"}, "同じ事象", "不明"))
    assert edge_correct("lineage", _j({"lineage"}, "同じ事象", "不明"), lenient=True)


def test_e2_use_thresholds() -> None:
    assert e2_use(60, 57)[1] == "書く材料"  # 0.95・下限 0.863
    assert e2_use(20, 19)[1] == "画面のみ"  # 0.95 だが下限 0.764
    assert e2_use(60, 50)[1] == "不可"  # 0.833
    assert e2_use(0, 0)[1] == "標本なし"


def test_e2_type_result_counts_subject_errors_among_wrong() -> None:
    js = [
        _j({"same_actor"}, "同じアクターの別の活動", "はい"),
        _j({"same_actor"}, "無関係", "いいえ", tag_a="誤り"),
        _j({"same_actor", "incident"}, "無関係", "不明"),
    ]
    res = e2_type_result("same_actor", js)
    assert (res.n, res.correct, res.wrong, res.wrong_with_subject_error) == (3, 1, 2, 1)
    assert res.unknown_attacker == 1
    assert e2_type_result("incident", js).n == 1


def test_present_order_and_blind_inputs_hide_ids() -> None:
    row = {
        "pair_key": "ev-1|ev-2",
        "a": {"event_id": "ev-1", "headline": "A"},
        "b": {"event_id": "ev-2", "headline": "B"},
        "types": ["incident"],
    }
    inputs, flip = blind_inputs(row)
    assert flip == present_order("ev-1|ev-2")
    assert "event_id" not in inputs["事象 1"]
    assert inputs["事象 1"]["headline"] == ("B" if flip else "A")


def test_to_judgement_maps_tags_back_when_flipped() -> None:
    row = {"pair_key": "a|b", "types": ["same_actor"]}
    v = EdgeVerdict(
        relation="無関係",
        same_attacker="いいえ",
        tag_1="誤り",
        tag_2="正しい",
        confidence="high",
        reason="r",
    )
    j = to_judgement(row, v, flip=True)
    assert (j.tag_a, j.tag_b) == ("正しい", "誤り")


# ------------------------------------------------------------ 標本


def test_pair_key_is_order_free() -> None:
    assert pair_key("b", "a") == pair_key("a", "b") == "a|b"


def test_resolve_root_follows_chain_and_stops_on_cycle() -> None:
    assert resolve_root("x", {"x": "y", "y": "z", "z": None}) == "z"
    assert resolve_root("p", {"p": "q", "q": "p"}) == "q"


def test_exclude_pairs_first_reason_wins() -> None:
    res = exclude_pairs(
        ["a|b", "c|d", "e|f", "g|h", "i|j"],
        excluded_pairs=frozenset({"a|b"}),
        excluded_events=frozenset({"a", "c"}),
        excluded_roots=frozenset({"R"}),
        root_of={"e": "R"},
        guarded_events=frozenset({"h"}),
    )
    assert res.kept == ["i|j"]
    assert res.dropped == {"prior_pair": 1, "prior_event": 1, "prior_root": 1, "guarded_article": 1}


def test_sample_pairs_one_event_per_type_and_deterministic() -> None:
    keys = ["a|b", "a|c", "a|d", "e|f", "g|h"]
    s1 = sample_pairs(keys, 5, 7)
    assert s1 == sample_pairs(list(reversed(keys)), 5, 7)
    events = [e for k in s1 for e in k.split("|")]
    assert len(events) == len(set(events))


def test_choose_sample_takes_all_when_small_and_keeps_must_include() -> None:
    assert choose_sample(["a|b", "a|c"], 60, 1) == ["a|b", "a|c"]
    keys = [f"x{i}|y{i}" for i in range(10)]
    out = choose_sample(keys, 3, 1, must_include=frozenset({"x9|y9"}))
    assert "x9|y9" in out and len(out) == 3


def test_pick_members_head_and_tail() -> None:
    assert pick_members(list("abcdefgh"), 6) == list("abcfgh")
    assert pick_members(list("ab"), 6) == ["a", "b"]
