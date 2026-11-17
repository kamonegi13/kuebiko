"""パターン版の関門 (ID・長さ・印との照合・固有名詞/数値の裏付け・禁止語)と決定論の文。"""

from __future__ import annotations

from src.synthesis.grounded.pattern_fallback import (
    QUIET_HEADLINE,
    fallback_headline,
    fallback_notable_text,
    fallback_pattern_text,
)
from src.synthesis.grounded.pattern_gates import (
    GateStats,
    gate_patterns,
    split_sentences,
    wording_violation,
)
from src.synthesis.grounded.patterns import PatternPlan, build_patterns
from tests.unit.synthesis_pattern_fixtures import BREACH, EXPLOIT, JP, attrs, j


def _plan() -> PatternPlan:
    js = [
        j("s-1", "アルファ証券で顧客情報が漏えいした。"),
        j("s-2", "ベータ銀行で委託先のシステム経由の漏えいがあった。"),
        j("s-3", "ガンマ商事で侵害が確認された。"),
    ]
    at = {
        "s-1": attrs(countries=JP, kinds=BREACH, s3=True),
        "s-2": attrs(countries=JP, kinds=BREACH),
        "s-3": attrs(countries=JP, kinds=BREACH),
    }
    return build_patterns(js, at)


def _gate(
    plan: PatternPlan,
    *,
    headline: str = "",
    items: list[tuple[str, str]] | None = None,
    watch: str = "",
) -> tuple[dict[str, str], str, GateStats]:
    gated, stats = gate_patterns(
        headline=headline, pattern_items=items or [], watch=watch, plan=plan
    )
    return gated.pattern_texts, gated.headline, stats


GOOD = "日本の侵害が 3 件あり、アルファ証券は S3 だが、ガンマ商事は侵害の確認にとどまる。"


def test_good_text_passes_unchanged() -> None:
    plan = _plan()
    texts, _, stats = _gate(plan, items=[("P1", GOOD)])
    assert texts["P1"] == GOOD
    assert stats.fields_replaced >= 0 and stats.missing == 0 and stats.unknown_ids == 0


def test_unknown_pattern_ids_are_dropped_and_missing_filled() -> None:
    plan = _plan()
    texts, _, stats = _gate(plan, items=[("P7", GOOD), ("P9", GOOD)])
    assert set(texts) == {"P1"}
    assert texts["P1"] == fallback_pattern_text(plan.patterns[0])
    assert (stats.unknown_ids, stats.missing) == (2, 1)


def test_duplicate_pattern_id_keeps_first() -> None:
    plan = _plan()
    texts, _, _ = _gate(plan, items=[("P1", GOOD), ("P1", "別の文。")])
    assert texts["P1"] == GOOD


def test_overlong_list_is_trimmed_to_first_two_sentences() -> None:
    plan = _plan()
    text = (
        "アルファ証券で漏えいがあった。ガンマ商事で侵害があった。"
        "ベータ銀行で漏えいがあった。3 件の動きがある。"
    )
    texts, _, stats = _gate(plan, items=[("P1", text)])
    assert texts["P1"] == "アルファ証券で漏えいがあった。ガンマ商事で侵害があった。"
    assert stats.length_trimmed == 1


def test_over_300_chars_even_after_trim_falls_back() -> None:
    plan = _plan()
    text = "アルファ証券" + "あ" * 160 + "。ガンマ商事" + "い" * 160 + "。"
    texts, _, stats = _gate(plan, items=[("P1", text)])
    assert texts["P1"] == fallback_pattern_text(plan.patterns[0])
    assert stats.length_replaced == 1


def test_route_wording_on_case_whose_claim_lacks_it_is_rejected() -> None:
    plan = _plan()
    # アルファ証券の claim に委託先は無い
    bad = "アルファ証券と、委託先経由のベータ銀行で漏えいがあった。"
    texts, _, stats = _gate(plan, items=[("P1", bad)])
    assert texts["P1"] == fallback_pattern_text(plan.patterns[0])
    assert stats.wording_replaced == 1


def test_route_wording_in_the_named_claim_passes() -> None:
    plan = _plan()
    ok = "ベータ銀行は委託先のシステム経由の漏えいだった。アルファ証券とは経路が異なる。"
    texts, _, stats = _gate(plan, items=[("P1", ok)])
    assert texts["P1"] == ok and stats.wording_replaced == 0


def test_route_wording_over_generalization_is_rejected() -> None:
    plan = _plan()
    texts, _, stats = _gate(plan, items=[("P1", "3 件はいずれも委託先経由の漏えいである。")])
    assert texts["P1"] == fallback_pattern_text(plan.patterns[0])
    assert stats.wording_replaced == 1


def test_route_wording_with_no_claim_saying_it() -> None:
    js = [j("s-1", "アルファ証券で漏えい。"), j("s-2", "ガンマ商事で侵害。")]
    plan = build_patterns(js, {k: attrs(kinds=BREACH) for k in ("s-1", "s-2")})
    assert wording_violation("提供者経由の動きが相次ぐ。", plan.patterns[0].cases)
    assert wording_violation("サードパーティ経由の動き。", plan.patterns[0].cases)
    assert not wording_violation("動きが相次ぐ。", plan.patterns[0].cases)


def test_headline_naming_case_without_route_in_claim_is_rejected() -> None:
    plan = _plan()
    _, head, stats = _gate(plan, headline="アルファ証券など委託先経由の漏えいが相次ぐ。")
    assert head == fallback_headline(plan)
    assert stats.headline_replaced


def test_watch_may_use_standing_claims_for_grounding() -> None:
    js = [
        j("s-1", "アルファ証券で顧客情報が漏えいした。"),
        j("s-2", "ガンマ商事で侵害が確認された。"),
        j("s-9", "デルタ運輸の基幹システムが停止した。", delta="no_change"),
    ]
    plan = build_patterns(js, {k: attrs(kinds=BREACH) for k in ("s-1", "s-2")})
    gated, stats = gate_patterns(
        headline="", pattern_items=[], watch="デルタ運輸の復旧を注視する。", plan=plan
    )
    assert gated.watch == "デルタ運輸の復旧を注視する。" and not stats.watch_dropped
    gated, stats = gate_patterns(
        headline="", pattern_items=[], watch="イプシロン社の動向を注視する。", plan=plan
    )
    assert gated.watch == "" and stats.watch_dropped


def _notable_plan() -> PatternPlan:
    js = [
        j("s-1", "アルファ証券で顧客情報が漏えいした。"),
        j("s-2", "ガンマ商事で侵害が確認された。"),
        j("s-3", "ゼータ取引所で大規模な不正流出が発生し、利用者資産が失われた。"),
    ]
    at = {
        "s-1": attrs(kinds=BREACH),
        "s-2": attrs(kinds=BREACH),
        "s-3": attrs(kinds=EXPLOIT, s3=True),
    }
    return build_patterns(js, at)


def test_notable_item_is_gated_like_patterns_with_fallback() -> None:
    plan = _notable_plan()
    ok = "ゼータ取引所で不正流出が発生した。"
    texts, _, stats = _gate(plan, items=[("P1", GOOD2), ("S1", ok)])
    assert texts["S1"] == ok and stats.fields_total == 3
    texts, _, stats = _gate(plan, items=[("P1", GOOD2), ("S1", "デルタ社でも不正流出があった。")])
    assert texts["S1"] == fallback_notable_text(plan.notable[0])
    assert texts["S1"] == "ゼータ取引所で大規模な不正流出が発生し、利用者資産が失われた。"
    texts, _, stats = _gate(plan, items=[("P1", GOOD2)])
    assert texts["S1"] == fallback_notable_text(plan.notable[0]) and stats.missing == 1


GOOD2 = "アルファ証券とガンマ商事で侵害が確認された。"


def test_ungrounded_proper_noun_and_number_are_rejected() -> None:
    plan = _plan()
    texts, _, stats = _gate(plan, items=[("P1", "デルタ社でも同様の漏えいが 3 件あった。")])
    assert texts["P1"] == fallback_pattern_text(plan.patterns[0])
    texts, _, stats2 = _gate(plan, items=[("P1", "アルファ証券で 9999 件の漏えいがあった。")])
    assert texts["P1"] == fallback_pattern_text(plan.patterns[0])
    assert stats.grounding_replaced == 1 and stats2.grounding_replaced == 1


def test_banned_word_is_replaced() -> None:
    plan = _plan()
    texts, _, stats = _gate(plan, items=[("P1", "アルファ証券の漏えいは確実に拡大する。")])
    assert texts["P1"] == fallback_pattern_text(plan.patterns[0])
    assert stats.banned == 1


def test_watch_dropped_when_banned_else_kept() -> None:
    plan = _plan()
    gated, stats = gate_patterns(headline="", pattern_items=[], watch="間違いなく続く", plan=plan)
    assert gated.watch == "" and stats.watch_dropped
    gated, _ = gate_patterns(
        headline="", pattern_items=[], watch="漏えいの続報を注視する。", plan=plan
    )
    assert gated.watch == "漏えいの続報を注視する。"


def test_fallback_text_shape() -> None:
    p = _plan().patterns[0]
    text = fallback_pattern_text(p)
    assert text.startswith("日本の侵害・情報漏えい: 3 件 (うち S3 1)。")
    assert "アルファ証券で顧客情報が漏えいした" in text


def test_quiet_day_headline() -> None:
    plan = build_patterns([j("s-1", "単独の動き。")], {})
    assert fallback_headline(plan).startswith(QUIET_HEADLINE)
    assert fallback_headline(plan, ["日本の組織への金銭目的は悪化しているか"]).startswith(
        "動いた常設の問い"
    )


def test_split_sentences() -> None:
    assert split_sentences("あ。い。う") == ["あ。", "い。", "う"]


def test_quoted_possibility_and_confidence_labels_are_allowed() -> None:
    """事実の引用 (可能性があると公表) と確度の印は禁止語ではない (11-17 P2 1 回目)。"""
    plan = _plan()
    text = "アルファ証券は漏えいの可能性があると公表し、評価は低確度にとどまる。"
    texts, _, stats = _gate(plan, items=[("P1", text)])
    assert stats.banned == 0


def test_only_the_heaviest_case_is_designated_and_must_be_named() -> None:
    plan = _notable_plan()
    assert [c.judgment_ids for c in plan.headline_subjects] == [("s-3",)]
    _, head, stats = _gate(plan, headline="ゼータ取引所の不正流出が目立つ。")
    assert head.startswith("ゼータ取引所の不正流出が目立つ。") and not stats.subject_replaced
    _, head, stats = _gate(plan, headline="アルファ証券とガンマ商事の漏えいが続く。")
    assert head == fallback_headline(plan) and stats.subject_replaced


def test_second_heaviest_s3_notable_is_appended_not_a_reason_to_replace() -> None:
    js = [
        j("s-1", "アルファ証券で顧客情報が漏えいした。"),
        j("s-2", "ガンマ商事で侵害が確認された。"),
        j("s-3", "ゼータ取引所で大規模な不正流出が発生した。"),
        j("s-4", "イータ運輸で業務停止を伴う攻撃が確認された。"),
    ]
    at = {
        "s-1": attrs(kinds=BREACH),
        "s-2": attrs(kinds=BREACH),
        "s-3": attrs(s3=True, large=True),
        "s-4": attrs(s3=True),
    }
    plan = build_patterns(js, at)
    assert plan.headline_subjects[0].judgment_ids == ("s-3",)
    assert plan.headline_extra is not None and plan.headline_extra.judgment_ids == ("s-4",)
    _, head, stats = _gate(plan, headline="ゼータ取引所の不正流出が目立つ。")
    assert (
        head == "ゼータ取引所の不正流出が目立つ。注目: イータ運輸で業務停止を伴う攻撃が確認された。"
    )
    assert stats.extra_appended and not stats.subject_replaced
    named = "ゼータ取引所とイータ運輸の事案が目立つ。"
    _, head, stats = _gate(plan, headline=named)
    assert head == named and not stats.extra_appended


def test_fallback_headline_is_short_without_counts() -> None:
    plan = _notable_plan()
    head = fallback_headline(plan)
    assert head.startswith("注目の単独事案: ゼータ取引所で大規模な不正流出が発生し")
    assert "注目: アルファ証券で顧客情報が漏えいした" in head
    assert "件" not in head and "S3" not in head
    assert len(split_sentences(head)) <= 2
    p_plan = _plan()
    assert len(split_sentences(fallback_headline(p_plan))) <= 2


def test_notable_c_id_is_accepted_as_alias_of_its_s_id() -> None:
    plan = _notable_plan()
    c_id = plan.notable[0].case.case_id
    assert c_id.startswith("C")
    ok = "ゼータ取引所で不正流出が発生した。"
    texts, _, stats = _gate(plan, items=[("P1", GOOD2), (c_id, ok)])
    assert texts["S1"] == ok and stats.unknown_ids == 0 and stats.missing == 0


def _method_plan() -> PatternPlan:
    js = [j("s-1", "甲社で侵害が確認された。"), j("s-2", "乙社で侵害が確認された。")]
    at = {
        "s-1": attrs(kinds=BREACH, techniques=frozenset({"T1566"})),
        "s-2": attrs(kinds=BREACH, techniques=frozenset({"T1566"})),
    }
    return build_patterns(js, at, {"T1566": "Phishing"})


def test_common_method_claim_needs_matching_mark_words() -> None:
    plan = _method_plan()
    bad = "甲社と乙社はいずれも同じ手口で侵害された。"
    texts, _, stats = _gate(plan, items=[("P1", bad)])
    assert texts["P1"] == fallback_pattern_text(plan.patterns[0]) and stats.method_replaced == 1
    ok = "甲社と乙社は共通して T1566 の手口で侵害された。"
    texts, _, stats = _gate(plan, items=[("P1", ok)])
    assert texts["P1"] == ok and stats.method_replaced == 0
    plain = "甲社と乙社で侵害が確認された。"  # 共通の手口は主張していない
    texts, _, _ = _gate(plan, items=[("P1", plain)])
    assert texts["P1"] == plain


def test_common_method_claim_without_any_mark_is_rejected() -> None:
    js = [j("s-1", "甲社で侵害。"), j("s-2", "乙社で侵害。")]
    plan = build_patterns(js, {k: attrs(kinds=BREACH) for k in ("s-1", "s-2")})
    texts, _, stats = _gate(plan, items=[("P1", "甲社と乙社は共通の手口だった。")])
    assert stats.method_replaced == 1 and texts["P1"] == fallback_pattern_text(plan.patterns[0])
