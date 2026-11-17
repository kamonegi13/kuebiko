"""パターン版の関門 (ID・長さ・印との照合・固有名詞/数値の裏付け・禁止語)と決定論の文。"""

from __future__ import annotations

from src.synthesis.grounded.pattern_fallback import (
    QUIET_HEADLINE,
    fallback_headline,
    fallback_pattern_text,
)
from src.synthesis.grounded.pattern_gates import (
    GateStats,
    gate_patterns,
    mark_violation,
    split_sentences,
)
from src.synthesis.grounded.patterns import PatternPlan, build_patterns
from tests.unit.synthesis_pattern_fixtures import BREACH, JP, attrs, j


def _plan() -> PatternPlan:
    js = [
        j("s-1", "アルファ証券で顧客情報が漏えいした。"),
        j("s-2", "ベータ銀行で委託先のシステム経由の漏えいがあった。"),
        j("s-3", "ガンマ商事で侵害が確認された。"),
    ]
    at = {
        "s-1": attrs(countries=JP, kinds=BREACH, s3=True),
        "s-2": attrs(countries=JP, kinds=BREACH, provider=True),
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


def test_provider_wording_on_case_without_mark_is_rejected() -> None:
    plan = _plan()
    bad = "アルファ証券と、委託先経由のベータ銀行で漏えいがあった。"  # アルファ証券は印なし
    texts, _, stats = _gate(plan, items=[("P1", bad)])
    assert texts["P1"] == fallback_pattern_text(plan.patterns[0])
    assert stats.mark_replaced == 1


def test_provider_wording_on_marked_case_only_passes() -> None:
    plan = _plan()
    ok = "ベータ銀行は委託先経由の漏えいだった。アルファ証券とは経路が異なる。"
    texts, _, stats = _gate(plan, items=[("P1", ok)])
    assert texts["P1"] == ok and stats.mark_replaced == 0


def test_provider_over_generalization_is_rejected() -> None:
    plan = _plan()
    texts, _, stats = _gate(plan, items=[("P1", "3 件はいずれも委託先経由の漏えいである。")])
    assert texts["P1"] == fallback_pattern_text(plan.patterns[0])
    assert stats.mark_replaced == 1


def test_provider_wording_when_no_case_has_mark() -> None:
    js = [j("s-1", "アルファ証券で漏えい。"), j("s-2", "ガンマ商事で侵害。")]
    plan = build_patterns(js, {k: attrs(kinds=BREACH) for k in ("s-1", "s-2")})
    assert mark_violation("提供者経由の動きが相次ぐ。", plan.patterns[0].cases)


def test_headline_naming_unmarked_case_with_provider_wording_is_rejected() -> None:
    plan = _plan()
    _, head, stats = _gate(plan, headline="アルファ証券など委託先経由の漏えいが相次ぐ。")
    assert head == fallback_headline(plan)
    assert stats.headline_replaced


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
    assert text.startswith("日本の侵害・情報漏えい: 3 件 (うち S3 1・提供者経由 1)。")
    assert "アルファ証券で顧客情報が漏えいした" in text


def test_quiet_day_headline() -> None:
    plan = build_patterns([j("s-1", "単独の動き。")], {})
    assert fallback_headline(plan).startswith(QUIET_HEADLINE)
    assert fallback_headline(plan, ["日本の組織への金銭目的は悪化しているか"]).startswith(
        "動いた常設の問い"
    )


def test_split_sentences() -> None:
    assert split_sentences("あ。い。う") == ["あ。", "い。", "う"]
