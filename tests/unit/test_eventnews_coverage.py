"""facts が原文のどこまで届いたかの計測 (src/eventnews/coverage.py)。

早期打ち切りの**観測**が目的。関門ではないので、測れないケースを
「届いていない」と読まないことが要件になる。
"""

from __future__ import annotations

from src.eventnews import coverage, generator
from src.eventnews.models import FactItem


def _fact(text: str, source_index: int = 1) -> FactItem:
    return FactItem(text=text, source_index=source_index, paragraph=1, section="what")


def _body(head: str, tail: str) -> str:
    """head と tail の間を埋めて MIN_BODY_CHARS を確実に超える原文を作る。"""
    return head + ("この段落は本文の中間を埋めるための説明である。" * 60) + tail


def test_reaching_the_end_of_the_source_yields_high_coverage() -> None:
    # Arrange
    body = _body("冒頭で事案の概要が報じられた。", "被害は合計2564件と発表された。")
    facts = [_fact("被害は合計2564件と発表された")]

    # Act
    result = coverage.deepest_coverage(facts, {1: body})

    # Assert
    assert result is not None
    assert result > 0.9


def test_stopping_at_the_opening_yields_low_coverage() -> None:
    # Arrange
    body = _body("冒頭で事案の概要が報じられた。", "被害は合計2564件と発表された。")
    facts = [_fact("冒頭で事案の概要が報じられた")]

    # Act
    result = coverage.deepest_coverage(facts, {1: body})

    # Assert
    assert result is not None
    assert result < coverage.WARN_BELOW


def test_short_sources_are_not_measured() -> None:
    # Arrange — MIN_BODY_CHARS 未満は「最後まで読めた」扱い
    facts = [_fact("短い記事の冒頭の一文である")]

    # Act
    result = coverage.deepest_coverage(facts, {1: "短い記事の冒頭の一文である。"})

    # Assert
    assert result is None


def test_returns_none_when_no_fragment_matches() -> None:
    # Arrange — 英語原文 × 日本語 fact のように、言い換えで一致が取れない場合
    body = _body("Attackers breached the portal.", "A total of 2,564 records leaked.")
    facts = [_fact("攻撃者が政府ポータルを侵害したと報じられた")]

    # Act
    result = coverage.deepest_coverage(facts, {1: body})

    # Assert — 「届いていない」ではなく「測れない」。警告に化けてはいけない
    assert result is None


def test_takes_the_deepest_source_when_multiple() -> None:
    # Arrange — 出典 2 は冒頭のみ、出典 1 は末尾まで
    deep = _body("出典1の冒頭である。", "出典1の末尾で被害規模が示された。")
    shallow = _body("出典2の冒頭である。", "出典2の末尾は使われていない。")
    facts = [
        _fact("出典1の末尾で被害規模が示された", source_index=1),
        _fact("出典2の冒頭である", source_index=2),
    ]

    # Act
    result = coverage.deepest_coverage(facts, {1: deep, 2: shallow})

    # Assert
    assert result is not None
    assert result > 0.9


def test_ignores_facts_whose_source_index_has_no_body() -> None:
    # Arrange — 範囲外の source_index (関門が別途弾くが、計測側も落ちないこと)
    body = _body("冒頭である。", "末尾で被害規模が示された。")
    facts = [_fact("末尾で被害規模が示された", source_index=9)]

    # Act
    result = coverage.deepest_coverage(facts, {1: body})

    # Assert
    assert result is None


def test_single_report_gets_the_full_body_cap() -> None:
    # Arrange / Act — 8,721 字の調査レポートを 30% しか渡していなかったのが真因
    cap = generator.body_cap(1)

    # Assert
    assert cap == generator._MEMBER_BODY_CHAR_MAX


def test_body_budget_is_shared_across_sources() -> None:
    # Arrange / Act
    caps = {n: generator.body_cap(n) for n in (2, 4, 8)}

    # Assert — 総量予算を超えず、単調に減る
    assert caps[2] > caps[4] > caps[8]
    assert all(
        n * cap <= generator._PROMPT_BODY_BUDGET
        for n, cap in caps.items()
        if cap > generator._MEMBER_BODY_CHAR_MIN
    )


def test_per_source_cap_never_drops_below_the_floor() -> None:
    # Arrange / Act — 複数報道で 1 件も潰さないための下限
    cap = generator.body_cap(50)

    # Assert
    assert cap == generator._MEMBER_BODY_CHAR_MIN
