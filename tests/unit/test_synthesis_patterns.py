"""横断パターンのまとめ (決定論・純粋関数)。"""

from __future__ import annotations

from src.storage.repo_judgment_attrs import JudgmentAttrs
from src.synthesis.grounded.estimate import KeyJudgment
from src.synthesis.grounded.patterns import build_patterns
from tests.unit.synthesis_pattern_fixtures import BREACH, EXPLOIT, JP, attrs, j


def _jp_breaches() -> tuple[list[KeyJudgment], dict[str, JudgmentAttrs]]:
    js = [
        j("s-1", "アルファ証券で顧客情報が漏えいした。詳細は調査中。"),
        j("s-2", "ベータ銀行で委託先経由の漏えいがあった。"),
        j("s-3", "ガンマ商事で侵害が確認された。"),
    ]
    at = {
        "s-1": attrs(countries=JP, kinds=BREACH, s3=True),
        "s-2": attrs(countries=JP, kinds=BREACH),
        "s-3": attrs(countries=JP, kinds=BREACH),
    }
    return js, at


def test_groups_by_region_and_kind_with_marks() -> None:
    js, at = _jp_breaches()
    plan = build_patterns(js, at)
    assert len(plan.patterns) == 1
    p = plan.patterns[0]
    assert (p.pattern_id, p.region, p.kind, p.count) == ("P1", "日本", "breach", 3)
    assert (p.s3_count, p.weight) == (1, 5)
    assert dict(p.delta_counts) == {"opened": 3}
    assert plan.rest == ()


def test_singleton_key_goes_to_rest() -> None:
    js, at = _jp_breaches()
    js.append(j("s-4", "デルタ社の脆弱性が悪用された。"))
    at["s-4"] = attrs(kinds=EXPLOIT)
    plan = build_patterns(js, at)
    assert [c.judgment_ids for c in plan.rest] == [("s-4",)]


def test_missing_attrs_or_kind_is_rest() -> None:
    plan = build_patterns(
        [j("s-1", "属性なしの動き。"), j("s-2", "助言のみ。")],
        {"s-2": attrs(kinds=frozenset({"advisory"}))},
    )
    assert plan.patterns == ()
    assert len(plan.rest) == 2


def test_judgments_sharing_event_root_merge_into_one_case() -> None:
    js = [
        j("s-1", "エプシロン社で侵害。"),
        j("s-2", "エプシロン社の侵害の続報。"),
        j("s-3", "ゼータ社で侵害。"),
    ]
    at = {
        "s-1": attrs(kinds=BREACH, roots=("ev-1",)),
        "s-2": attrs(kinds=BREACH, roots=("ev-1", "ev-9")),
        "s-3": attrs(kinds=BREACH, roots=("ev-3",)),
    }
    plan = build_patterns(js, at)
    p = plan.patterns[0]
    assert p.count == 2  # s-1 と s-2 は 1 事案
    merged = next(c for c in p.cases if len(c.judgment_ids) == 2)
    assert set(merged.judgment_ids) == {"s-1", "s-2"}
    assert len(merged.claims) == 2


def test_japan_first_then_weight() -> None:
    js = [j(f"s-{n}", f"事案{n}") for n in range(1, 7)]
    at = {
        "s-1": attrs(countries=JP, kinds=BREACH),
        "s-2": attrs(countries=JP, kinds=BREACH),
        "s-3": attrs(kinds=BREACH, s3=True),
        "s-4": attrs(kinds=BREACH, s3=True),
        "s-5": attrs(kinds=EXPLOIT),
        "s-6": attrs(kinds=EXPLOIT),
    }
    plan = build_patterns(js, at)
    assert [(p.region, p.kind) for p in plan.patterns] == [
        ("日本", "breach"),
        ("海外", "breach"),  # S3 を含み重い
        ("海外", "exploitation"),
    ]
    assert [p.pattern_id for p in plan.patterns] == ["P1", "P2", "P3"]


def test_japan_related_flag_makes_region_japan() -> None:
    js = [j("s-1", "a。", japan=True), j("s-2", "b。", japan=True)]
    at = {"s-1": attrs(kinds=BREACH), "s-2": attrs(kinds=BREACH)}
    assert build_patterns(js, at).patterns[0].region == "日本"


def test_no_change_judgments_are_standing_not_patterned() -> None:
    js = [j(f"s-{n}", f"継続{n}", delta="no_change") for n in range(6)]
    at = {f"s-{n}": attrs(kinds=BREACH) for n in range(6)}
    plan = build_patterns(js, at)
    assert plan.patterns == () and plan.rest == ()
    assert len(plan.standing) == 4
    assert plan.moved_count == 0
    assert plan.top_judgment is not None


def test_deterministic_regardless_of_input_order() -> None:
    js, at = _jp_breaches()
    assert build_patterns(js, at) == build_patterns(list(reversed(js)), at)


def test_short_title_cuts_at_comma_after_min_chars_and_caps() -> None:
    from src.synthesis.grounded.patterns import short_title

    assert short_title("短い題。続き。") == "短い題"
    assert short_title("あ" * 20 + "、" + "い" * 10 + "、う。") == "あ" * 20 + "、" + "い" * 10
    assert short_title("あ" * 24 + "、い、う。") == "あ" * 24 + "、い"  # 25 字以降の最初の「、」
    assert short_title("あ" * 70 + "。") == "あ" * 59 + "…"
    assert short_title("あ" * 30 + "、後半。") == "あ" * 30


def test_s3_singleton_becomes_notable_and_leaves_rest() -> None:
    js = [
        j("s-1", "事案一。"),
        j("s-2", "事案二。"),
        j("s-3", "重大な単独事案。"),
        j("s-4", "軽い動き。"),
    ]
    at = {
        "s-1": attrs(kinds=BREACH),
        "s-2": attrs(kinds=BREACH),
        "s-3": attrs(kinds=EXPLOIT, s3=True),
        "s-4": attrs(),
    }
    plan = build_patterns(js, at)
    assert [(n.item_id, n.case.judgment_ids) for n in plan.notable] == [("S1", ("s-3",))]
    assert [c.judgment_ids for c in plan.rest] == [("s-4",)]


def test_notable_is_capped_at_two() -> None:
    js = [j(f"s-{n}", f"単独{n}。") for n in range(4)]
    at = {f"s-{n}": attrs(s3=True) for n in range(4)}
    plan = build_patterns(js, at)
    assert [n.item_id for n in plan.notable] == ["S1", "S2"]
    assert len(plan.rest) == 2
