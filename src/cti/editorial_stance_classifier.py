"""editorial_stance の **focused 単機能分類器** (2026-06-28)。

背景: editorial_stance を summarizer (過負荷の 26B、多数フィールド同時出力) に任せると
geopolitical を unknown 連発 (実測 1-3/10)。stance 1 タスクに絞った小プロンプトなら 10/10 安定。
→ summarizer から切り出し、**取得時に専用 LLM 呼び出し**で確実に付与する (CLAUDE.md §3 の
「過負荷モデルに分類を足さない」原則)。pipeline (forward) と backfill の両方がこの 1 実装を使う。

ローカル LLM のみ。source 名でなく内容 (rhetorical 性質) で判定する。
"""

from __future__ import annotations

from pydantic import BaseModel

from src.cti.llm_routing_flags import EditorialStance
from src.logging_config import get_logger
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

_BODY_CHARS = 1500


class _StanceOut(BaseModel):
    editorial_stance: EditorialStance


# 判定基準 (summarizer.j2 と同義だが stance 単機能に凝縮)。地政学の客観報道を
# factual_report に倒すのが要点 (summarizer は unknown に逃げていた)。
_PROMPT = """次の記事の編集スタンスを判定。**source 名でなく内容 (rhetorical 性質) で判定**。
- factual_report: 客観的な事実・事象報道 (who/what/when/where)。サイバー advisory/CVE/breach に加え
  **地政学/政策の客観報道も** (軍事配備・外交・紛争・人事・声明・主張/発表)。地政学の既定。
- analytical: シンクタンク/研究機関の中立な分析・解説・technical writeup。
- opinion: 著者の主観を含む論評 (論理的議論)。
- propaganda: 特定国家/組織を擁護し敵対勢力を貶める一方的 framing (検証なき断定・framing 用語等)。
  国営メディアでも内容が客観報道なら factual_report。
- unknown: 本文が無い/極端に短い時のみ。本文があれば避ける。**迷ったら factual_report**。

タイトル: {title}
本文: {body}

JSON {{"editorial_stance"}} のみ返答。"""


async def classify_editorial_stance(llm: LLMClient, title: str, body: str) -> EditorialStance:
    """記事 1 本の editorial_stance を focused に判定する。障害時は "unknown" (安全側)。"""
    text = (body or "").strip()
    if not text:
        return "unknown"
    prompt = _PROMPT.format(title=(title or "").strip(), body=text[:_BODY_CHARS])
    try:
        res = await llm.generate_structured(prompt, schema=_StanceOut, think=False)
    except Exception as e:  # noqa: BLE001 — 分類失敗で記事処理を止めない
        _log.warning("editorial_stance_classify_failed", error=str(e)[:80])
        return "unknown"
    return res.editorial_stance
