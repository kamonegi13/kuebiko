"""C: LLM リランク。候補を query との真の関連度で採点・並べ替え (精度の決定打)。

embedding の限界 (テーマ一致止まり) を、LLM が query 意図と記事内容を突き合わせて補正する。
1 LLM call で候補一括採点 (batched)。fail-safe (LLM 不調→None、呼び出し側は融合順を維持)。
"""

from __future__ import annotations

from src.logging_config import get_logger
from src.search.models import RerankOutput
from src.storage.run_history import ArticleRecord
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

_SUMMARY_MAX = 240
_RERANK_MAX_TOKENS = 2000


def _build_prompt(query: str, candidates: list[ArticleRecord]) -> str:
    lines: list[str] = []
    for i, a in enumerate(candidates):
        meta = " / ".join(
            x for x in [a.importance or "", a.category or "", a.feed_title or ""] if x
        )
        summary = (a.summary or "")[:_SUMMARY_MAX]
        lines.append(f"[{i}] {a.title}\n    ({meta})\n    {summary}".rstrip())
    body = "\n".join(lines)
    return (
        "あなたは CTI アナリストの検索を支援する。検索クエリに対し、各候補記事が\n"
        "**本当に関連するか** を 0-10 で採点せよ (テーマが被るだけは低め、クエリの\n"
        "核心に合致するほど高く)。\n\n"
        f"検索クエリ: {query}\n\n"
        f"候補 (番号付き):\n{body}\n\n"
        "JSON のみ出力 (純粋な JSON):\n"
        '{"items": [{"index": 0, "score": 9, "reason": "20字程度で根拠"}, ...]}\n'
        "全候補を採点する。score は 0 (無関係) - 10 (完全一致)。\n"
    )


async def rerank(
    llm: LLMClient,
    query: str,
    candidates: list[ArticleRecord],
) -> RerankOutput | None:
    """候補を LLM で採点。失敗時 None (融合順を維持)。"""
    if not candidates:
        return RerankOutput(items=[])
    try:
        out = await llm.generate_structured(
            _build_prompt(query, candidates),
            schema=RerankOutput,
            think=False,
            max_tokens=_RERANK_MAX_TOKENS,
            temperature=0.0,
        )
        _log.info("rerank_done", candidates=len(candidates), scored=len(out.items))
        return out
    except Exception as e:  # noqa: BLE001
        _log.warning("rerank_failed", error=str(e))
        return None
