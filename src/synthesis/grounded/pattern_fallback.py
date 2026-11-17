"""パターン版の状況総括の決定論の文 (関門で LLM の欄を落としたとき・静かな日)。設計 §3.3 / §3.4。"""

from __future__ import annotations

from collections.abc import Sequence

from src.synthesis.grounded.patterns import NotableCase, Pattern, PatternCase, PatternPlan
from src.synthesis.grounded.slot_render_extras import FIRST_SENTENCE_MAX_CHARS, first_sentence

#: 決定論の文に並べる事案の題の上限 (超えたら「ほか N 件」)
TITLES_MAX = 5
#: 見出しの決定論の文で名指しする、最も重いパターンの事案の数
HEADLINE_CASES = 2
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
    return first_sentence(claim, FIRST_SENTENCE_MAX_CHARS).rstrip("。") + "。"


def fallback_headline(plan: PatternPlan, moved_questions: Sequence[str] = ()) -> str:
    """見出しの決定論の文。パターンがあれば重い順に 2 つまで。無ければ静かな日の文。"""
    notable = f"注目の単独事案: {_bare(plan.notable[0].case.title)}。" if plan.notable else ""
    if plan.patterns:
        top = plan.patterns[0]
        named = "、".join(_bare(c.title) for c in top.cases[:HEADLINE_CASES])
        text = f"{top.region}の{top.kind_label}が {pattern_marks(top)} 動いた。主な事案: {named}。"
        if len(plan.patterns) > 1:
            nxt = plan.patterns[1]
            text += f"次いで{nxt.region}の{nxt.kind_label}が {nxt.count} 件。"
        return text + notable
    if notable:
        return QUIET_HEADLINE + notable
    if moved_questions:
        return "動いた常設の問い: " + "、".join(moved_questions) + "。"
    head = plan.top_judgment
    if head is None:
        return NO_MOVE_HEADLINE
    return f"{QUIET_HEADLINE}最も重い動き: {_bare(first_sentence(head.claim))}。"
