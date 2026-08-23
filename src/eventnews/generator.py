"""事象ニュースの生成 (docs/event_news_design.md §9)。

メンバー選抜 → prompt 組み立て → structured 生成、の 3 段。識別子関門
(``src/tools/identifier_match.py`` 予定、別 worktree 分担) はここで呼ばない —
runner が ``generate_draft`` の戻り値に対して関門を適用し ``GateResult`` を組む。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import jinja2

from src.cti.source_basis import classify_source_tier
from src.eventnews.models import PROMPT_MEMBER_CAP, EventNewsDraft, MemberArticle
from src.tools.llm_client import LLMClient

_PROMPT_TEMPLATE = "eventnews/refine.j2"
_PROMPTS_DIR = Path("prompts")

# member 1 件あたり title/feed_title/summary の切詰め上限 (§9 の記載どおり各 1400 字)
_MEMBER_FIELD_CHAR_CAP = 1400

# select_members の tier 優先度 (official > research > その他)。未列挙 (news/social/
# state_media/unknown) はすべて同格の「その他」— tier 内での序列は anchor_ts のみで決める。
_SELECT_TIER_RANK: dict[str, int] = {"official": 0, "research": 1}
_SELECT_TIER_OTHER = 2


def select_members(
    members: Sequence[MemberArticle],
) -> tuple[list[MemberArticle], int]:
    """prompt へ渡すメンバーを選抜する (§9)。

    序列は tier (official > research > その他) → anchor_ts 昇順。上限 ``PROMPT_MEMBER_CAP``。
    戻り値は (選抜されたメンバー, 省略件数)。
    """

    def _sort_key(m: MemberArticle) -> tuple[int, object]:
        tier = classify_source_tier(m.feed_title, m.feed_url)
        return (_SELECT_TIER_RANK.get(tier, _SELECT_TIER_OTHER), m.anchor_ts)

    ordered = sorted(members, key=_sort_key)
    selected = ordered[:PROMPT_MEMBER_CAP]
    omitted = len(members) - len(selected)
    return selected, omitted


def _prompt_env() -> jinja2.Environment:
    # spotlight/generator.py の直接組み立て fallback 経路と同じ流儀 (composer 非統合、§9)。
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_PROMPTS_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
    )


def build_prompt(members: Sequence[MemberArticle], allowed_identifiers_text: str) -> str:
    """``prompts/eventnews/refine.j2`` を render する (§9)。

    渡すコンテキスト: 番号付きメンバー (title/feed_title/anchor/summary、各 1400 字切詰め) /
    省略件数 / 使ってよい識別子一覧 (呼び手が整形済みの文字列をそのまま渡す)。
    """
    selected, omitted = select_members(members)
    numbered = [
        {
            "index": i,
            "title": m.title[:_MEMBER_FIELD_CHAR_CAP],
            "feed_title": m.feed_title[:_MEMBER_FIELD_CHAR_CAP],
            "anchor": m.anchor_ts.isoformat(),
            "summary": m.summary[:_MEMBER_FIELD_CHAR_CAP],
        }
        for i, m in enumerate(selected, start=1)
    ]
    template = _prompt_env().get_template(_PROMPT_TEMPLATE)
    return template.render(
        members=numbered,
        omitted=omitted,
        allowed_identifiers_text=allowed_identifiers_text,
    )


async def generate_draft(
    members: Sequence[MemberArticle],
    allowed_identifiers_text: str,
    llm: LLMClient,
) -> EventNewsDraft:
    """事象ニュースの structured 出力を生成する (§9、Step.EVENT_NEWS / fast tier)。

    識別子関門・[N] 関門はここで呼ばない (runner が ``GateResult`` として統合する)。
    ``llm`` は呼出側が ``model_tiers.build_llm_for(Step.EVENT_NEWS, config)`` 等で
    組み立てて渡す — モデル名をここでハードコードしない。
    """
    prompt = build_prompt(members, allowed_identifiers_text)
    return await llm.generate_structured(
        prompt=prompt,
        schema=EventNewsDraft,
        temperature=0.2,
        think=False,
    )
