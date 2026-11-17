"""パターン版の状況総括の節の組み立て (決定論)。設計 docs/synthesis_pattern_redesign.md §3.5。

``_WireSections`` の欄は据え置き、中身だけ差し替える (画面・Discord は欄名で描くので変更不要)。
"""

from __future__ import annotations

from src.synthesis.grounded.estimate import Estimate, KeyJudgment
from src.synthesis.grounded.pattern_case import PatternCase
from src.synthesis.grounded.pattern_fallback import NO_MOVE_HEADLINE
from src.synthesis.grounded.pattern_gates import GatedTexts
from src.synthesis.grounded.patterns import NO_CHANGE_TYPES, Pattern, PatternPlan, StandingItem
from src.synthesis.grounded.render import _DELTA_JA, _pir_titles, _WireSections
from src.synthesis.grounded.slot_render import (
    NO_PIR,
    NO_STANDING,
    RELATIONS_NOT_USED,
    _spillover,
)

NO_PATTERN_WEIGHT = "横断のパターンは立たなかった (同じ地域・種別で 2 件以上の動きがない)。"
_CONF_JA = {"high": "高確度", "moderate": "中確度", "low": "低確度"}


def _case_line(c: PatternCase) -> str:
    delta = _DELTA_JA.get(c.delta_type, c.delta_type)
    conf = _CONF_JA.get(c.confidence, c.confidence)
    return f"- {c.title.strip().rstrip('。')} ({delta}・{conf})"


def _pattern_block(p: Pattern, text: str) -> str:
    head = f"【{p.region}の{p.kind_label}】"
    marks = "、".join(m.label for m in p.common_marks)
    method = [f"共通の手口: {marks}"] if marks else []
    return "\n".join([f"{head}{text}", *method, *(_case_line(c) for c in p.cases)])


def weight_section(plan: PatternPlan, gated: GatedTexts) -> str:
    blocks = [_pattern_block(p, gated.pattern_texts[p.pattern_id]) for p in plan.patterns]
    for n in plan.notable:
        blocks.append(f"【注目の単独事案】{gated.pattern_texts[n.item_id]}\n{_case_line(n.case)}")
    if plan.rest:
        blocks.append("\n".join(["そのほかの動き:", *(_case_line(c) for c in plan.rest)]))
    return "\n\n".join(blocks) or NO_PATTERN_WEIGHT


def chain_section(plan: PatternPlan) -> str:
    """共通の実体 (被害組織・製品・CVE 等) を持つ事案の組 + パターン間の S3 の共有。

    決定論だけ。因果・関係は創作しない。無ければ「扱っていない」。
    """
    lines = [
        f"- {link.a.title} と {link.b.title}: 共通 = {'、'.join(link.shared)}"
        for link in plan.links
    ]
    sharing = [(p, p.s3_count) for p in plan.patterns if p.s3_count > 0]
    if len(sharing) >= 2:
        detail = "、".join(f"{p.region}の{p.kind_label} {n} 件" for p, n in sharing)
        lines.append(f"- 深刻度 S3 の事案を含むパターン: {detail}。")
    return "\n".join(lines) or RELATIONS_NOT_USED


def pir_counts_section(est: Estimate) -> str:
    """SIR ごとに動いた判定の件数だけ (判定の原文は並べない)。"""
    titles = _pir_titles()
    moved: dict[str, int] = {}
    opened: dict[str, int] = {}
    for j in est.judgments:
        if j.delta_type in NO_CHANGE_TYPES:
            continue
        for pid in j.pir_ids:
            moved[pid] = moved.get(pid, 0) + 1
            opened[pid] = opened.get(pid, 0) + (j.delta_type == "opened")
    lines = [
        f"- {titles.get(pid, pid)}: 動いた判定 {n} 件 (うち新規 {opened[pid]})"
        for pid, n in sorted(moved.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return "\n".join(lines) or NO_PIR


def _shown(est: Estimate, plan: PatternPlan) -> list[KeyJudgment]:
    ids = {i for p in plan.patterns for c in p.cases for i in c.judgment_ids}
    ids |= {i for c in plan.rest for i in c.judgment_ids}
    ids |= {i for n in plan.notable for i in n.case.judgment_ids} | {j.id for j in plan.standing}
    return [j for j in est.judgments if j.id in ids]


def standing_line(item: StandingItem) -> str:
    when = f"・最終 {item.last_moved}" if item.last_moved else ""
    return f"- {item.title} ({_CONF_JA.get(item.confidence, item.confidence)}{when})"


def cog_section(plan: PatternPlan) -> str:
    """継続中 (動いていない) の判定を全件 1 行ずつ。台帳の長い claim は写さない。"""
    return "\n".join(standing_line(i) for i in plan.standing_items) or NO_STANDING


def assemble_pattern_sections(
    *, est: Estimate, plan: PatternPlan, gated: GatedTexts
) -> _WireSections:
    return _WireSections(
        headline=gated.headline or NO_MOVE_HEADLINE,
        weight_section=weight_section(plan, gated),
        chain_section=chain_section(plan),
        cog_section=cog_section(plan),
        spillover_section=_spillover(gated.watch, _shown(est, plan)),
        pir_section=pir_counts_section(est),
    )
