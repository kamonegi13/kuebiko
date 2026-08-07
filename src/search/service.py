"""検索オーケストレーション。

quick:   B (hybrid retrieval) のみ。LLM 不使用で ~1-2s。
precise: D (plan) → B (retrieval) → C (rerank)。LLM 2 call で高精度。
全層 fail-safe で degrade。
"""

from __future__ import annotations

import re

from src.cti.actor_normalizer import ActorAliasRegistry, load_actor_aliases
from src.cti.diamond_model import SOCIO_POLITICAL_INTENTS
from src.search.models import LlmQueryPlan, RetrievalPlan, SearchFacets, SearchHit, SearchResponse
from src.search.planner import plan_query
from src.search.reranker import rerank
from src.search.retriever import retrieve_candidates
from src.storage.run_history import RunHistoryRepository
from src.tools.embedding_client import EmbeddingClient
from src.tools.llm_client import LLMClient

_SUMMARY_MAX = 300
_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,7}$", re.IGNORECASE)
_IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9-]+\.)+[a-z]{2,}$", re.IGNORECASE)
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$", re.IGNORECASE)
_SHA1_RE = re.compile(r"^[a-f0-9]{40}$", re.IGNORECASE)
_MD5_RE = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)


def _detect_entity(query: str) -> tuple[str, str] | None:
    """構造化エンティティ (CVE/IP/ドメイン/ハッシュ) を検出 (deterministic)。"""
    s = query.strip()
    if not s or " " in s:
        return None
    if _CVE_RE.match(s):
        return ("cve", s.upper())
    if _IP_RE.match(s):
        return ("ioc_ip", s)
    if _SHA256_RE.match(s):
        return ("ioc_sha256", s.lower())
    if _SHA1_RE.match(s):
        return ("ioc_sha1", s.lower())
    if _MD5_RE.match(s):
        return ("ioc_md5", s.lower())
    if _DOMAIN_RE.match(s):
        return ("ioc_domain", s.lower())
    return None


def _actor_entity_hints(
    query: str,
    llm_actors: list[str],
    registry: ActorAliasRegistry | None = None,
) -> list[tuple[str, str]]:
    """actor 名 (別名込み) を辞書で canonical id に解決して entity hint 化する。

    article_entities の actor は canonical id (例: ``apt10``) で保存されるため、
    "Cicada" / "Stone Panda" のような別名のままでは structured leg が一致しない。
    query 本文の別名検出 (quick mode でも効く) と LLM plan の actor 名解決の両方を
    行い、辞書未収載の名前は従来通り raw で渡す (degrade)。
    """
    reg = registry if registry is not None else load_actor_aliases()
    hints: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        v = value.strip().lower()
        if v and v not in seen:
            seen.add(v)
            hints.append(("actor", v))

    for actor in reg.find_all(query):
        _add(actor.id)
    for name in llm_actors:
        if not name.strip():
            continue
        resolved = reg.find(name)
        _add(resolved.id if resolved is not None else name)
    return hints


def _build_retrieval_plan(
    query: str,
    llm_plan: LlmQueryPlan | None,
    registry: ActorAliasRegistry | None = None,
) -> RetrievalPlan:
    entity_hints: list[tuple[str, str]] = []
    auto = _detect_entity(query)
    if auto is not None:
        entity_hints.append(auto)

    if llm_plan is None:
        # quick: raw クエリを keyword + semantic に (actor 別名は id に解決して structured leg へ)
        entity_hints.extend(_actor_entity_hints(query, [], registry))
        return RetrievalPlan(
            semantic_query=query,
            keywords=[query],
            entity_hints=entity_hints,
        )

    entity_hints.extend(_actor_entity_hints(query, list(llm_plan.actors), registry))
    for c in llm_plan.cves:
        if c.strip():
            entity_hints.append(("cve", c.strip()))
    intent = llm_plan.intent if llm_plan.intent in SOCIO_POLITICAL_INTENTS else ""
    return RetrievalPlan(
        semantic_query=llm_plan.semantic_query.strip() or query,
        keywords=llm_plan.keywords or [query],
        category_hints=llm_plan.categories,
        entity_hints=entity_hints,
        intent_hint=intent,
    )


async def search(
    repo: RunHistoryRepository,
    *,
    query: str,
    mode: str = "precise",
    embedder: EmbeddingClient | None = None,
    llm: LLMClient | None = None,
    facets: SearchFacets | None = None,
    limit: int = 20,
    top_k: int = 60,
) -> SearchResponse:
    """統合検索。mode='quick'(hybrid のみ) / 'precise'(plan+rerank 込み)。

    ``facets`` を渡すと query 駆動の候補に hard filter を AND 合成する (browse facet と
    同一語彙)。空 query + facet のみは browse 側 (``/articles``) の責務なので扱わない。
    """
    q = query.strip()
    if not q:
        return SearchResponse(query=query, mode=mode, embeddings_available=embedder is not None)

    llm_plan: LlmQueryPlan | None = None
    if mode == "precise" and llm is not None:
        llm_plan = await plan_query(llm, q)

    rplan = _build_retrieval_plan(q, llm_plan)
    candidates = await retrieve_candidates(repo, embedder, plan=rplan, facets=facets, top_k=top_k)

    rerank_scores: dict[str, tuple[float, str]] = {}
    reranked = False
    order = candidates
    if mode == "precise" and llm is not None and candidates:
        rr = await rerank(llm, q, [c[0] for c in candidates])
        if rr is not None and rr.items:
            for it in rr.items:
                if 0 <= it.index < len(candidates):
                    rerank_scores[candidates[it.index][0].article_id] = (it.score, it.reason)
            order = sorted(
                candidates,
                key=lambda c: (rerank_scores.get(c[0].article_id, (-1.0, ""))[0], c[1]),
                reverse=True,
            )
            reranked = True

    hits: list[SearchHit] = []
    for rec, fused, via in order[:limit]:
        rs = rerank_scores.get(rec.article_id)
        hits.append(
            SearchHit(
                article_id=rec.article_id,
                title=rec.title,
                url=rec.url,
                importance=rec.importance,
                category=rec.category,
                feed_title=rec.feed_title,
                socio_political_intent=rec.socio_political_intent,
                intent_confidence=rec.intent_confidence,
                summary=(rec.summary or "")[:_SUMMARY_MAX] or None,
                created_at=rec.created_at.isoformat() if rec.created_at else None,
                score=round(fused, 5),
                rerank_score=(rs[0] if rs else None),
                reason=(rs[1] if rs else ""),
                matched_via=via,
            )
        )
    return SearchResponse(
        query=q,
        mode=mode,
        count=len(hits),
        results=hits,
        reranked=reranked,
        embeddings_available=embedder is not None,
        plan=llm_plan,
    )
