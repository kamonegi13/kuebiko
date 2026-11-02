"""eventnews-v7 (anchor つき) の生成と関門 (n20 T5、旗 ``FACT_ANCHOR``)。

- on: ``generator.generate_draft`` がここへ委ねる。v7 で生成 → anchor の関門 → (留保語の脱落が
  あれば 1 回だけ作り直し) → 違反の行を落とし anchor を捨てた ``EventNewsDraft`` を返す。
  以降 (識別子関門・転載の関門・版の記録) は v6 と同じ経路。全行が落ちたら ``LLMError``
  (runner が「版を書かない」に倒す — 空の本文は出せない)。
- shadow: 本番 (v6) の版を書いた後に v7 を別に生成し、関門の件数だけを log に残す
  (``eventnews_fact_anchor_shadow``。本文・anchor は log に出さない)。配信は変わらない。
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from src.eventnews import fact_anchor
from src.eventnews.generator import (
    _PROMPT_TEMPLATE_V7,
    EVENT_NEWS_MAX_TOKENS,
    build_prompt,
    prompt_member_texts,
    select_members,
)
from src.eventnews.list_dedup import dedup_draft
from src.eventnews.models import EventNewsDraft, MemberArticle
from src.tools.llm_client import LLMClient, LLMError

_log = structlog.get_logger(__name__)

PROMPT_VERSION_V7 = "eventnews-v7"


async def _call(llm: LLMClient, prompt: str) -> fact_anchor.AnchoredEventNewsDraft:
    out: fact_anchor.AnchoredEventNewsDraft = await llm.generate_structured(
        prompt=prompt,
        schema=fact_anchor.AnchoredEventNewsDraft,
        temperature=0.2,
        max_tokens=EVENT_NEWS_MAX_TOKENS,
        think=False,
    )
    return out


async def generate_anchored_draft(
    members: Sequence[MemberArticle],
    allowed_identifiers_text: str,
    llm: LLMClient,
    *,
    rewrite_hint: str = "",
) -> EventNewsDraft:
    """v7 で生成し、anchor の関門を通した本番の形 (anchor なし) を返す。"""
    selected, _ = select_members(members)
    texts = prompt_member_texts(selected)
    prompt = build_prompt(
        members, allowed_identifiers_text, rewrite_hint=rewrite_hint, template=_PROMPT_TEMPLATE_V7
    )
    raw = await _call(llm, prompt)
    probe = fact_anchor.apply_anchor_gate(raw, texts, drop=False)
    retried = False
    if probe.hedge_issues:
        retried = True
        again = await _call(llm, prompt + "\n\n" + fact_anchor.hedge_rewrite_hint(probe))
        if len(fact_anchor.apply_anchor_gate(again, texts, drop=False).issues) < len(probe.issues):
            raw = again
    result = fact_anchor.apply_anchor_gate(raw, texts, drop=True)
    _log.info(
        "eventnews_fact_anchor_gate",
        dropped=result.dropped,
        retried=retried,
        issues=fact_anchor.issue_counts(result.issues),
        facts=len(result.draft.facts),
    )
    if not result.draft.facts:
        raise LLMError("fact_anchor: all facts dropped by the anchor gate")
    draft, removed = dedup_draft(result.draft)
    if removed:
        _log.warning("eventnews_list_dedup", removed=removed)
    return draft


async def shadow_measure(
    selected: Sequence[MemberArticle],
    allowed_identifiers_text: str,
    llm: LLMClient,
    *,
    item_id: str,
) -> None:
    """shadow: v7 を別に生成して関門の件数だけ記録する。失敗は log のみ (本番を止めない)。"""
    try:
        texts = prompt_member_texts(select_members(selected)[0])
        prompt = build_prompt(selected, allowed_identifiers_text, template=_PROMPT_TEMPLATE_V7)
        raw = await _call(llm, prompt)
        result = fact_anchor.apply_anchor_gate(raw, texts, drop=True)
    except Exception as e:  # noqa: BLE001 — shadow は配信に影響させない
        _log.warning("eventnews_fact_anchor_shadow_failed", item_id=item_id, error=str(e)[:160])
        return
    rows = sum(len(getattr(raw, s)) for s in fact_anchor.ANCHORED_SECTIONS)
    _log.info(
        "eventnews_fact_anchor_shadow",
        item_id=item_id,
        rows=rows,
        dropped=result.dropped,
        issues=fact_anchor.issue_counts(result.issues),
        facts_after=len(result.draft.facts),
    )
