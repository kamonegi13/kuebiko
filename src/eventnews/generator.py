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

# member 1 件あたり title/feed_title の切詰め上限
_MEMBER_FIELD_CHAR_CAP = 1400
# 本文の切詰め上限 (2026-08-23 の A/B 実測で決定)。
# **入力は per-article 要約でなく記事本文**。要約は本文の 1/12.8 (336 字 vs 4,313 字) まで
# 圧縮済みで、事象統合の段階では失われた情報を回復できない。本文入力で情報量 +48% /
# facts +26% (4 事象・同一プロンプト同一モデルでの実測)。プロンプト側で分量を要求する
# 案 (v2) は +10% にとどまり、梃子は入力側だった。費用は 14 秒/件で要約入力と同等。
_MEMBER_BODY_CHAR_MIN = 2600
# 1 事象ぶんの本文の総量。members が増えても prompt が線形に膨らまないよう総量で持つ。
_PROMPT_BODY_BUDGET = 20000
# 単独報の長い調査レポートを最後まで載せるための 1 記事あたり上限。
_MEMBER_BODY_CHAR_MAX = 12000


def body_cap(member_count: int) -> int:
    """1 記事に割り当てる本文の文字数。

    2026-08-26 実測: 固定 2,600 字は**単独報の 8,721 字の記事を 30% しか見せて
    おらず**、後半にあった被害規模 (「1,000 万件」「2,000 件超流出」) を落とした上、
    それを unknowns へ「不明」と書いていた — モデルの選抜ミスではなく、渡していない
    範囲を正直に「不明」と答えていた。生成が到達した位置の実測 (27% / 57%) は、
    cap による可視率 (30% / 61%) とほぼ一致した。**指示ではなく入力で直す**。
    """
    if member_count <= 0:
        return _MEMBER_BODY_CHAR_MIN
    share = _PROMPT_BODY_BUDGET // member_count
    return max(_MEMBER_BODY_CHAR_MIN, min(_MEMBER_BODY_CHAR_MAX, share))


# 本文抽出をすり抜けた媒体側の定型見出し。LLM がこれを事実の一部として写す実害が
# 出た (The Register の "MORE CONTEXT" が「CONTEXT の文脈として」という本文になった)。
# 指示で止めず入力側で断つ (禁止は指示では止まらない、2026-08-19 の規約)。
_BOILERPLATE_MARKERS: tuple[str, ...] = (
    "MORE CONTEXT",
    "MORE ON THIS",
    "READ MORE",
    "RELATED STORIES",
    "SPONSORED",
    "ADVERTISEMENT",
)


def _strip_boilerplate(text: str) -> str:
    out = text
    for marker in _BOILERPLATE_MARKERS:
        out = out.replace(marker, " ")
    return out


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

    **本文も要約も持たないメンバーは選抜しない** (2026-08-23)。重複判定で落ちた記事
    (``status='skipped_duplicate'``) は要約前に落ちるため 241/245 が本文・要約とも空で、
    タイトルだけを [N] 枠として提示すると LLM が「その記事に書かれた事実」を創作する
    (実測: 要約なしメンバーのみの 4 事象すべてで CVE 番号の捏造)。裏取り媒体としては
    ``compute_source_breakdown`` が別途数えるので、独立媒体数は減らない。
    """

    def _sort_key(m: MemberArticle) -> tuple[int, object]:
        tier = classify_source_tier(m.feed_title, m.feed_url)
        return (_SELECT_TIER_RANK.get(tier, _SELECT_TIER_OTHER), m.anchor_ts)

    textual = [m for m in members if (m.body or m.summary).strip()]
    ordered = sorted(textual, key=_sort_key)
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


def build_prompt(
    members: Sequence[MemberArticle],
    allowed_identifiers_text: str,
    *,
    rewrite_hint: str = "",
) -> str:
    """``prompts/eventnews/refine.j2`` を render する (§9)。

    渡すコンテキスト: 番号付きメンバー (title/feed_title/anchor/**本文** ``body_cap``
    字で切詰め、本文が無ければ要約) / 省略件数 / 使ってよい識別子一覧 (呼び手が整形済み)。
    """
    selected, omitted = select_members(members)
    cap = body_cap(len(selected))
    numbered = [
        {
            "index": i,
            "title": m.title[:_MEMBER_FIELD_CHAR_CAP],
            "feed_title": m.feed_title[:_MEMBER_FIELD_CHAR_CAP],
            "anchor": m.anchor_ts.isoformat(),
            "summary": _strip_boilerplate(m.body or m.summary)[:cap],
        }
        for i, m in enumerate(selected, start=1)
    ]
    template = _prompt_env().get_template(_PROMPT_TEMPLATE)
    return template.render(
        members=numbered,
        omitted=omitted,
        # 単独報は「統合」ではなく 1 記事の再構成。節ごとの要約で読み物にする
        # (文単位の [N] は出典が 1 つしかないので情報を持たない)。
        solo=len(selected) == 1,
        # 書き直しの理由 (runner が組み立てる。空なら通常生成)。
        rewrite_hint=rewrite_hint,
        allowed_identifiers_text=allowed_identifiers_text,
    )


async def generate_draft(
    members: Sequence[MemberArticle],
    allowed_identifiers_text: str,
    llm: LLMClient,
    *,
    rewrite_hint: str = "",
) -> EventNewsDraft:
    """事象ニュースの structured 出力を生成する (§9、Step.EVENT_NEWS / narrative tier)。

    識別子関門・[N] 関門はここで呼ばない (runner が ``GateResult`` として統合する)。
    ``llm`` は呼出側が ``model_tiers.build_llm_for(Step.EVENT_NEWS, config)`` 等で
    組み立てて渡す — モデル名をここでハードコードしない。
    """
    prompt = build_prompt(members, allowed_identifiers_text, rewrite_hint=rewrite_hint)
    return await llm.generate_structured(
        prompt=prompt,
        schema=EventNewsDraft,
        temperature=0.2,
        think=False,
    )
