"""パターン版の状況総括の決定論の文 (関門で LLM の欄を落としたとき・静かな日)。設計 §3.3 / §3.4。"""

from __future__ import annotations

from collections.abc import Sequence

from src.synthesis.grounded.pattern_case import PatternCase, first_full_sentence
from src.synthesis.grounded.patterns import NotableCase, Pattern, PatternPlan

#: 決定論の文に並べる事案の題の上限 (超えたら「ほか N 件」)
TITLES_MAX = 5
QUIET_HEADLINE = "確度をもって報告できる横断の動きはない。"
NO_MOVE_HEADLINE = "本期間に確度ある主要判定は得られなかった。"


def _bare(title: str) -> str:
    return title.strip().rstrip("。")


def _titles(cases: Sequence[PatternCase]) -> str:
    shown = "、".join(_bare(c.title) for c in cases[:TITLES_MAX])
    more = len(cases) - TITLES_MAX
    return f"{shown}、ほか {more} 件" if more > 0 else shown


def pattern_marks(p: Pattern) -> str:
    return f"{p.count} 件 (うち S3 {p.s3_count})"


def fallback_pattern_text(p: Pattern) -> str:
    """「{地域}の{種別}: {件数} 件 (うち S3 {n})。{事案の短い題}、…」。"""
    return f"{p.region}の{p.kind_label}: {pattern_marks(p)}。{_titles(p.cases)}。"


def fallback_notable_text(n: NotableCase) -> str:
    """注目の単独事案の決定論の文 = 判定の claim の第 1 文。"""
    return _sentence_of(n.case.claims[0])


def _sentence_of(claim: str) -> str:
    return first_full_sentence(claim) + "。"


def _pattern_label(plan: PatternPlan, case: PatternCase) -> str:
    for p in plan.patterns:
        if case in p.cases:
            return f"{p.region}の{p.kind_label}: "
    return "注目の単独事案: "


def _second_case(plan: PatternPlan, top: PatternCase) -> PatternCase | None:
    """見出しの 2 件目 = 2 番目に重い S3 の注目の単独事案、無ければ次に重い事案。"""
    if plan.headline_extra is not None:
        return plan.headline_extra
    others = [
        c
        for c in (*(x for p in plan.patterns for x in p.cases), *(n.case for n in plan.notable))
        if c != top
    ]
    return max(others, key=lambda c: (c.weight, -c.rank), default=None)


def fallback_headline(plan: PatternPlan, moved_questions: Sequence[str] = ()) -> str:
    """見出しの決定論の文。最も重い事案 + 高々もう 1 件、2 文以内、件数は書かない。

    事案が無い日は静かな日の文。
    """
    if plan.headline_subjects:
        top = plan.headline_subjects[0]
        text = f"{_pattern_label(plan, top)}{_bare(top.title)}。"
        second = _second_case(plan, top)
        return text + (f"注目: {_bare(second.title)}。" if second else "")
    if moved_questions:
        return "動いた常設の問い: " + "、".join(moved_questions) + "。"
    head = plan.top_judgment
    if head is None:
        return NO_MOVE_HEADLINE
    return f"{QUIET_HEADLINE}最も重い動き: {first_full_sentence(head.claim)}。"
