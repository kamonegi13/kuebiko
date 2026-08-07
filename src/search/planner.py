"""D: LLM クエリ理解。NL クエリを構造化検索プランに翻訳する (soft-boost)。

出力は全て「hint」として retriever の追加 leg + 加点に使う (ハードフィルタにしない)
ため、過剰絞り込みによるゼロ件事故が構造的に起きない。fail-safe (LLM 不調→None)。
"""

from __future__ import annotations

from src.cti.diamond_model import SOCIO_POLITICAL_INTENTS
from src.logging_config import get_logger
from src.search.models import LlmQueryPlan
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

_CATEGORIES = (
    "vulnerability, advisory, breach, incident, malware, apt, apt_leak, "
    "phishing, policy, geopolitical, research, recap, other"
)
_INTENTS = ", ".join(i for i in SOCIO_POLITICAL_INTENTS if i != "unknown")
_PLAN_MAX_TOKENS = 600


def _build_prompt(query: str) -> str:
    return (
        "あなたは CTI 検索の前処理を行う。日本語の検索クエリを、記事 DB を引くための\n"
        "構造化プランに翻訳せよ。**絞り込みではなく検索のヒント**を出す (広めに)。\n\n"
        f"クエリ: {query}\n\n"
        "以下を JSON で出力 (純粋な JSON のみ):\n"
        "- semantic_query: 意味検索用に整えた1文 (クエリの言い換え/明確化)\n"
        "- keywords: 記事本文に出そうな検索語を **日本語と英語の両方** で 3-8 個 "
        '(例: 中国の通信侵入 → ["通信", "telecom", "carrier", "侵入", "breach", "中国"])\n'
        f"- categories: 該当しそうな category を 0-3 個 (候補: {_CATEGORIES})\n"
        "- actors: 関係しそうな脅威アクター名/別名を 0-5 個 "
        "(例: Salt Typhoon, Volt Typhoon, lazarus)\n"
        "- cves: クエリに CVE があれば (なければ空)\n"
        f"- intent: 攻撃者の意図が明確なら 1 つ (候補: {_INTENTS}、不明なら空)\n\n"
        "{\n"
        '  "semantic_query": "...",\n'
        '  "keywords": ["..."],\n'
        '  "categories": ["..."],\n'
        '  "actors": ["..."],\n'
        '  "cves": ["..."],\n'
        '  "intent": "..."\n'
        "}\n"
    )


async def plan_query(llm: LLMClient, query: str) -> LlmQueryPlan | None:
    """NL クエリ → LlmQueryPlan。失敗時 None (呼び出し側は raw クエリで継続)。"""
    try:
        plan = await llm.generate_structured(
            _build_prompt(query),
            schema=LlmQueryPlan,
            think=False,
            max_tokens=_PLAN_MAX_TOKENS,
            temperature=0.0,
        )
        _log.info(
            "query_planned",
            keywords=len(plan.keywords),
            categories=plan.categories,
            actors=len(plan.actors),
        )
        return plan
    except Exception as e:  # noqa: BLE001
        _log.warning("query_plan_failed", error=str(e))
        return None
