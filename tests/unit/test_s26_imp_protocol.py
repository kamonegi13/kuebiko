"""scripts/s26_imp の純関数のテスト (指示の組み立て・境の型の選択・一貫性の検査・多数決・関門・回数の上限)。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from scripts.s26_imp import common as c
from scripts.s26_imp.opus import BudgetExceededError, OpusBudget, judge_items
from scripts.s26_imp.protocol import (
    PrecheckItem,
    RelabelRow,
    TeacherRow,
    agreement,
    flags_consistency,
    gold_vote,
    high_criteria_numbers,
    majority_vote,
    needs_second_pass,
    needs_third,
    precheck_metrics,
    relabel_gates,
    run_teacher_protocol,
)

SECTION = (
    "  【サイバーの記事】\n  1. 実環境で悪用中の脆弱性\n  2. 被害が確認され、複数組織に及ぶ侵害\n"
)


def j(
    imp: str,
    criterion: str = "",
    orgs: int | None = None,
    by: str = "無し",
    form: str = "事案の報道",
) -> dict[str, Any]:
    return {
        "id": "x",
        "importance": imp,
        "criterion": criterion,
        "reason": "r",
        "confirmed_victim_orgs": orgs,
        "confirmed_by": by,
        "article_form": form,
    }


# ---- 指示 ----
def test_instruction_embeds_importance_section_verbatim() -> None:
    text = c.build_instruction(SECTION)
    assert SECTION.rstrip() in text
    assert text.index(SECTION.rstrip()) < text.index("## 出力")


def test_instruction_has_extra_fields_and_boundary_only_on_request() -> None:
    plain = c.build_instruction(SECTION)
    withb = c.build_instruction(SECTION, with_boundary=True)
    for f in ("confirmed_victim_orgs", "confirmed_by", "article_form"):
        assert f in plain
    assert "boundary" not in plain
    assert "boundary" in withb
    assert c.instruction_sha(plain) != c.instruction_sha(withb)


def test_rubric_id_in_cache_rubric() -> None:
    assert c.RUBRIC_ID == "s26-imp-v8r7"
    assert "s26-imp-v8r7" in c.rubric_for_cache(c.build_instruction(SECTION))


def test_instruction_matches_real_v8r7_section_when_available() -> None:
    p = c.DATA_ROOT / c.RUBRIC_V8R7_REL
    if not p.exists():
        pytest.skip("data/ が無い")
    body = c.load_importance_section(p)
    assert body.rstrip() in c.build_instruction(body)


def test_question_differs_by_pass() -> None:
    assert c.question_for_pass(1) == c.QUESTION
    assert c.question_for_pass(2) != c.question_for_pass(3) != c.question_for_pass(1)


def test_roundup_title_rule() -> None:
    assert c.is_roundup_row(None, "", "Weekly Threat Roundup")
    assert c.is_roundup_row("通常の見出し", "Week in Review: stuff")
    assert not c.is_roundup_row("A ransomware gang hit a hospital", None)


def test_batching_four_and_char_cap() -> None:
    items = [{"id": str(i), "title": "t", "feed": "f", "body": "x" * 10} for i in range(9)]
    assert [len(g) for g in c.batch_items(items)] == [4, 4, 1]
    big = [{"id": str(i), "title": "t", "feed": "f", "body": "x" * 30_000} for i in range(3)]
    assert [len(g) for g in c.batch_items(big)] == [1, 1, 1]
    _, labels = c.batch_inputs(items[:2])
    assert labels == {"記事1": "0", "記事2": "1"}


# ---- 境の型の選択 ----
@pytest.mark.parametrize(
    "crit", ["high-2", "high-3: 国家系", "high-5", "high-6", "high-8 (暗号資産)"]
)
def test_second_pass_for_boundary_high_criteria(crit: str) -> None:
    assert needs_second_pass(j("high", crit))


@pytest.mark.parametrize("crit", ["high-1", "high-4", "high-7", "high-25", "high-12"])
def test_no_second_pass_for_other_high(crit: str) -> None:
    assert not needs_second_pass(j("high", crit))


def test_high_number_parsing_does_not_match_prefix() -> None:
    assert high_criteria_numbers("high-25 / high-2") == {25, 2}
    assert high_criteria_numbers("medium: 単一組織") == set()


@pytest.mark.parametrize("cue", ["単一組織の侵害", "分析記事", "国家系の活動", "主張のみ"])
def test_second_pass_for_medium_cues(cue: str) -> None:
    assert needs_second_pass(j("medium", f"medium: {cue}"))
    assert not needs_second_pass(j("low", f"low: {cue}"))


def test_second_pass_plain_medium_is_skipped() -> None:
    assert not needs_second_pass(j("medium", "medium: 脆弱性の修正のお知らせ"))


def test_second_pass_when_consistency_check_hits() -> None:
    assert needs_second_pass(j("medium", "medium: その他", orgs=3, by="当局"))


# ---- 一貫性の検査 ----
def test_check_i_multi_org_confirmed_not_high() -> None:
    assert flags_consistency(j("medium", "m", orgs=2, by="被害者")) == ("i",)
    assert flags_consistency(j("low", "m", orgs=5, by="独立研究者")) == ("i",)
    assert flags_consistency(j("medium", "m", orgs=1, by="被害者")) == ()
    assert flags_consistency(j("medium", "m", orgs=4, by="主張のみ")) == ()
    assert flags_consistency(j("high", "high-1", orgs=4, by="当局")) == ()
    assert flags_consistency(j("medium", "m", orgs=None, by="当局")) == ()


def test_check_ii_high2_without_confirmation() -> None:
    assert flags_consistency(j("high", "high-2", orgs=1, by="被害者")) == ("ii",)
    assert flags_consistency(j("high", "high-2", orgs=5, by="主張のみ")) == ("ii",)
    assert flags_consistency(j("high", "high-2", orgs=None, by="当局")) == ("ii",)
    assert flags_consistency(j("high", "high-2", orgs=3, by="当局")) == ()
    assert flags_consistency(j("high", "high-1", orgs=0, by="無し")) == ()
    assert flags_consistency(j("medium", "high-2 に近い", orgs=0, by="主張のみ")) == ()


# ---- 多数決 ----
def test_majority_single_and_agreeing() -> None:
    assert majority_vote([j("high")]).label == "high"
    assert majority_vote([j("medium"), j("medium")]) == majority_vote([j("medium")])


def test_majority_three_votes() -> None:
    v = majority_vote([j("high"), j("medium"), j("medium")])
    assert (v.label, v.boundary) == ("medium", False)
    v = majority_vote([j("high"), j("medium"), j("high")])
    assert (v.label, v.boundary) == ("high", False)


def test_majority_medium_low_third_value_decides() -> None:
    assert majority_vote([j("medium"), j("low"), j("low")]).label == "low"
    assert majority_vote([j("low"), j("medium"), j("medium")]).label == "medium"


def test_majority_three_way_split_is_medium_boundary() -> None:
    v = majority_vote([j("high"), j("medium"), j("low")])
    assert (v.label, v.boundary) == ("medium", True)


def test_needs_third_modes() -> None:
    assert needs_third([j("high"), j("medium")], any_disagreement=False)
    assert not needs_third([j("medium"), j("low")], any_disagreement=False)
    assert needs_third([j("medium"), j("low")], any_disagreement=True)
    assert not needs_third([j("high"), j("high")], any_disagreement=True)
    assert not needs_third([j("high")], any_disagreement=True)


def test_gold_vote_boundary_on_first_two_split() -> None:
    v = gold_vote([j("high"), j("medium"), j("high")])
    assert (v.label, v.boundary) == ("high", True)
    v = gold_vote([j("medium"), j("low")])
    assert v.boundary is False
    v = gold_vote([j("high"), j("high")])
    assert (v.label, v.boundary) == ("high", False)


# ---- 指標 ----
def trow(i: str, *labels: str, boundary: bool = False, flags: tuple[str, ...] = ()) -> TeacherRow:
    js = tuple(j(lb) for lb in labels)
    return TeacherRow(i, js, majority_vote(js).label, boundary, (), flags)


def test_agreement_counts_second_pass_rows_only() -> None:
    rows = [trow("a", "high"), trow("b", "high", "high"), trow("c", "high", "medium", "medium")]
    assert agreement(rows) == (1, 2)


def test_precheck_pass() -> None:
    items = [PrecheckItem(f"h{i}", "high", False, False, trow(f"h{i}", "high")) for i in range(30)]
    items += [
        PrecheckItem(f"m{i}", "medium", False, False, trow(f"m{i}", "medium")) for i in range(10)
    ]
    items += [PrecheckItem("s", "high", False, False, trow("s", "high", "high"))]
    m = precheck_metrics(items)
    assert m["verdict"] == "PASS"
    assert m["values"] == {"A1": 1.0, "A2": 1.0, "A3": 1.0}


def test_precheck_fail_on_a2() -> None:
    items = [PrecheckItem(f"h{i}", "high", False, False, trow(f"h{i}", "high")) for i in range(20)]
    items += [
        PrecheckItem(f"m{i}", "medium", False, False, trow(f"m{i}", "high")) for i in range(5)
    ]
    items += [PrecheckItem("s", "high", False, False, trow("s", "high", "high"))]
    m = precheck_metrics(items)
    assert m["verdict"] == "FAIL"
    assert m["passed"]["A1"] and not m["passed"]["A2"]


def test_precheck_frame_excludes_roundup_and_boundary() -> None:
    base = [PrecheckItem(f"h{i}", "high", False, False, trow(f"h{i}", "high")) for i in range(20)]
    base.append(PrecheckItem("s", "high", False, False, trow("s", "high", "high")))
    noise = [
        PrecheckItem("r", "medium", False, True, trow("r", "high")),  # まとめ記事: 枠の外
        PrecheckItem("b", "medium", True, False, trow("b", "high")),  # 境界: 枠の外
    ]
    m = precheck_metrics(base + noise)
    assert m["verdict"] == "PASS"
    assert m["frame"]["excluded_roundup"] == 1 and m["frame"]["excluded_boundary"] == 1
    assert m["unframed_record_only"]["A2"] < 1.0


def test_precheck_undetermined_with_few_gold_high() -> None:
    items = [PrecheckItem(f"h{i}", "high", False, False, trow(f"h{i}", "high")) for i in range(5)]
    items.append(PrecheckItem("s", "high", False, False, trow("s", "high", "high")))
    assert precheck_metrics(items)["verdict"] == "UNDETERMINED"


def rrow(
    k: str, old: str, t: TeacherRow, cat: str | None = "malware", roundup: bool = False
) -> RelabelRow:
    return RelabelRow(k, old, cat, roundup, t)


def test_relabel_gates_pass_and_b2_limit() -> None:
    rows = [rrow(f"k{i}", "medium", trow(f"k{i}", "medium")) for i in range(100)]
    rows.append(rrow("s", "high", trow("s", "high", "high")))
    rows += [
        rrow("f1", "low", trow("f1", "low", flags=("i",))),
        rrow("f2", "low", trow("f2", "low", flags=("ii",))),
    ]
    g = relabel_gates(rows, [], 994)
    assert g["verdict"] == "PASS"
    assert g["B2"]["limit"] == 19 and g["B2"]["flagged_after_vote"] == 2


def test_relabel_gates_fail_conditions() -> None:
    rows = [rrow("a", "high", trow("a", "high", "medium", "medium"))]
    assert relabel_gates(rows, [], 994)["passed"]["B1"] is False
    flagged = [rrow(f"f{i}", "low", trow(f"f{i}", "low", flags=("i",))) for i in range(20)]
    assert relabel_gates(flagged, [], 994)["passed"]["B2"] is False
    assert relabel_gates([], ["lost"], 994)["passed"]["B3"] is False
    geo = [rrow("g", "medium", trow("g", "high"), cat="geopolitical")]
    assert relabel_gates(geo, [], 994)["passed"]["B3"] is False


def test_relabel_b1_excludes_roundup_and_boundary() -> None:
    rows = [
        rrow("a", "high", trow("a", "high", "high")),
        rrow("r", "high", trow("r", "high", "medium", "medium"), roundup=True),
        rrow("b", "high", trow("b", "high", "medium", "low", boundary=True)),
    ]
    g = relabel_gates(rows, [], 994)
    assert g["B1"]["second_pass_rows"] == 1 and g["B1"]["value"] == 1.0
    assert g["record_only"]["roundup_rows_high"] == 0


# ---- 回数の上限 ----
def test_budget_stops_at_1_3x() -> None:
    b = OpusBudget("X", 10)
    assert b.limit == 13
    b.charge(13)
    with pytest.raises(BudgetExceededError):
        b.charge(1)


# ---- 手順の駆動 (偽の判定役) ----
def fake_judge(table: dict[str, list[tuple[str, str]]]) -> Any:
    """title → [(importance, criterion)] を 1 回ごとに消費する。"""
    calls: list[int] = []

    async def judge(_q: str, inputs: dict[str, Any], _tag: str) -> Any:
        from scripts.s26_imp.common import BatchVerdict, ItemVerdict

        calls.append(len(inputs["articles"]))
        items = []
        for a in inputs["articles"]:
            imp, crit = table[a["title"]].pop(0)
            items.append(
                ItemVerdict(
                    id=a["id"],
                    importance=imp,  # type: ignore[arg-type]
                    criterion=crit,
                    reason="r",
                    confirmed_victim_orgs=None,
                    confirmed_by="無し",
                    article_form="その他",
                )
            )
        return BatchVerdict(items=items)

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


def test_teacher_protocol_second_and_third_passes() -> None:
    items = [{"id": f"id{i}", "title": f"t{i}", "feed": "f", "body": "b"} for i in range(6)]
    table = {
        "t0": [("high", "high-1")],  # 1 回のみ
        "t1": [
            ("high", "high-2"),
            ("medium", "medium: x"),
            ("medium", "medium: x"),
        ],  # 割れ → 3 回目 medium
        "t2": [("medium", "medium: 分析"), ("medium", "medium: 分析")],  # 一致
        "t3": [("medium", "medium: 単一組織"), ("low", "low")],  # medium/low の食い違い → 3 回目
        "t4": [("low", "low")],
        "t5": [("high", "high-6"), ("high", "high-6")],
    }
    table["t3"].append(("low", "low"))
    judge = fake_judge(table)
    res = asyncio.run(run_teacher_protocol(items, judge))
    assert not res.failed
    assert res.rows["id0"].label == "high" and len(res.rows["id0"].judgments) == 1
    assert res.rows["id1"].label == "medium" and len(res.rows["id1"].judgments) == 3
    assert len(res.rows["id2"].judgments) == 2
    assert res.rows["id3"].label == "low" and len(res.rows["id3"].judgments) == 3
    assert len(res.rows["id4"].judgments) == 1
    assert res.pass_sizes == {1: 6, 2: 4, 3: 2}


def test_judge_items_marks_failures_after_retries() -> None:
    async def never(_q: str, _i: dict[str, Any], _t: str) -> None:
        return None

    groups = [[{"id": "a", "title": "t", "feed": "f", "body": "b"}]]
    got, failed = asyncio.run(judge_items(groups, pass_no=1, judge=never))
    assert got == {} and failed == ["a"]
