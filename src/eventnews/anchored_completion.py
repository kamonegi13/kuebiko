"""eventnews-v7 の教師の completion を、本番と同じ関門を通した anchor つきの形に整える (n20)。

契約 (data/eval/n20_contract_2026_11_02.md §1): completion は identifier_gate と anchor の関門を
**通した後**で、facts・caveats・discrepancies の各行に anchor を持つ形。本番の経路
(``anchored_generator.generate_anchored_draft`` → runner の ``identifier_gate.verify_draft``) と
同じ順で当てる:

1. anchor の関門 (``fact_anchor.apply_anchor_gate(drop=True)``) — 違反の行を落とす
2. リスト欄の重複畳み (``list_dedup``、本番と同じ判定を text に当てる)
3. 識別子関門 (``identifier_gate.verify_draft``) — **anchor を除いた形にだけ当てる**
   (anchor は原文の逐語で生の CVE・IP を含むため、当てると anchor が壊れる)
4. 関門後の text・source_index に、元の行の anchor を戻す

本番の旗 off/on の戻り値は変えない (ここは教師の生成・検査だけが呼ぶ)。純粋関数、LLM なし。
"""

from __future__ import annotations

from collections.abc import Sequence

from src.eventnews import fact_anchor, identifier_gate
from src.eventnews.fact_anchor import AnchoredEventNewsDraft, AnchoredFactItem
from src.eventnews.generator import prompt_member_texts, select_members
from src.eventnews.list_dedup import dedup_indices, dedup_items
from src.eventnews.models import FactItem, MemberArticle


def _dedup_rows(rows: Sequence[AnchoredFactItem]) -> list[AnchoredFactItem]:
    return [rows[i] for i in dedup_indices([r.text for r in rows])]


def _reattach(
    gated: Sequence[FactItem], rows: Sequence[AnchoredFactItem]
) -> list[AnchoredFactItem]:
    return [
        AnchoredFactItem(
            text=g.text,
            source_index=g.source_index,
            paragraph=g.paragraph,
            section=g.section,
            anchor=r.anchor,
        )
        for g, r in zip(gated, rows, strict=True)
    ]


def anchored_completion(
    raw: AnchoredEventNewsDraft, members: Sequence[MemberArticle]
) -> AnchoredEventNewsDraft | None:
    """関門を通した anchor つきの draft。facts が 1 行も残らなければ ``None`` (本番は版を書かない)。

    ``members`` は ``build_prompt`` に渡したのと同じ列 (選抜はここで本番と同じに行う)。
    """
    selected, _ = select_members(members)
    gate = fact_anchor.apply_anchor_gate(raw, prompt_member_texts(selected), drop=True)
    kept = gate.anchored
    deduped = kept.model_copy(
        update={
            "key_points": dedup_items(list(kept.key_points)),
            "unknowns": dedup_items(list(kept.unknowns)),
            **{s: _dedup_rows(getattr(kept, s)) for s in fact_anchor.ANCHORED_SECTIONS},
        }
    )
    verified = identifier_gate.verify_draft(fact_anchor.to_plain_draft(deduped), selected).draft
    n_members = len(selected)
    fact_rows = [r for r in deduped.facts if identifier_gate.fact_row_kept(r.to_plain(), n_members)]
    if not verified.facts:
        return None
    return AnchoredEventNewsDraft(
        headline=verified.headline,
        bluf=verified.bluf,
        key_points=list(verified.key_points),
        facts=_reattach(verified.facts, fact_rows),
        discrepancies=_reattach(verified.discrepancies, deduped.discrepancies),
        caveats=_reattach(verified.caveats, deduped.caveats),
        unknowns=list(verified.unknowns),
    )
