"""B: 多信号 候補取得 (vector + keyword + structured) を RRF で融合。

各 leg は記事 id のランク付きリストを返し、Reciprocal Rank Fusion で統合する。
lexical/structured leg は embedding 未生成記事も対象なので被覆を補完する。
全 leg は fail-safe (失敗した leg は無視、他 leg で継続)。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC

from src.logging_config import get_logger
from src.search.models import RetrievalPlan, SearchFacets
from src.storage.run_history import ArticleRecord, RunHistoryRepository
from src.tools.embedding_client import EmbeddingClient

_log = get_logger(__name__)

RRF_K = 60  # RRF の定数 (標準値)。小さいほど上位の寄与が強い。
_PER_LEG_LIMIT = 40
_MAX_KEYWORDS = 6
_MAX_ENTITY_HINTS = 6

# structured leg で許可する値 (任意文字列を SQL に流さない)。
_ALLOWED_CATEGORIES = {
    "vulnerability",
    "advisory",
    "breach",
    "incident",
    "malware",
    "apt",
    "apt_leak",
    "phishing",
    "policy",
    "geopolitical",
    "research",
    "recap",
    "other",
}
_ALLOWED_ENTITY_TYPES = {
    "actor",
    "cve",
    "malware_family",
    "tool",
    "ttp",
    "ioc_ip",
    "ioc_domain",
    "ioc_url",
    "ioc_sha256",
    "ioc_sha1",
    "ioc_md5",
}


def _rrf(ranked_lists: list[list[str]]) -> dict[str, float]:
    """複数のランク付き id リストを Reciprocal Rank Fusion で統合。"""
    scores: dict[str, float] = defaultdict(float)
    for lst in ranked_lists:
        for rank, aid in enumerate(lst):
            scores[aid] += 1.0 / (RRF_K + rank + 1)
    return scores


def _facet_ok(rec: ArticleRecord, facets: SearchFacets, allowed_ids: set[str] | None) -> bool:
    """``rec`` が hard facet を全て満たすか (scalar 属性 + entity 集合)。"""
    if facets.importance and rec.importance != facets.importance:
        return False
    if facets.importance_in and rec.importance not in facets.importance_in:
        return False
    if facets.category and rec.category != facets.category:
        return False
    if facets.category_in and rec.category not in facets.category_in:
        return False
    if facets.feed_title and rec.feed_title != facets.feed_title:
        return False
    if facets.posted_channel and rec.posted_channel != facets.posted_channel:
        return False
    if (
        facets.socio_political_intent
        and rec.socio_political_intent != facets.socio_political_intent
    ):
        return False
    if facets.status and rec.status != facets.status:
        return False
    # 本文由来 facet (2026-07-27): stump=切り株 / full=全文取得済。
    _full_sources = ("full_extract", "playwright_extract", "prefetch", "scraper")
    if facets.body_source == "stump" and rec.body_source != "feed_summary":
        return False
    if facets.body_source == "full" and rec.body_source not in _full_sources:
        return False
    if facets.since is not None and rec.created_at is not None:
        ca = rec.created_at if rec.created_at.tzinfo else rec.created_at.replace(tzinfo=UTC)
        if ca < facets.since:
            return False
    return not (allowed_ids is not None and rec.article_id not in allowed_ids)


async def retrieve_candidates(
    repo: RunHistoryRepository,
    embedder: EmbeddingClient | None,
    *,
    plan: RetrievalPlan,
    facets: SearchFacets | None = None,
    top_k: int = 60,
) -> list[tuple[ArticleRecord, float, list[str]]]:
    """plan に基づき候補を取得し RRF 融合。``(ArticleRecord, fused_score, matched_via)`` を返す。

    ``facets`` を渡すと query 駆動の候補に hard filter を AND 合成する:
    keyword leg は facet 条件込みで引き (facet 内 recall を確保)、最終 fused 集合は
    ``_facet_ok`` で post-filter する (vector/structured leg 由来の facet 外候補も除去)。
    """
    ranked_lists: list[list[str]] = []
    by_id: dict[str, ArticleRecord] = {}
    matched_via: dict[str, set[str]] = defaultdict(set)

    # keyword leg に渡す facet 条件 (facets 未指定なら従来通り posted のみ)。
    leg_kwargs = facets.to_query_kwargs() if facets is not None else {"status": "posted"}
    # entity facet が立つ場合、各 filter の article_id 集合の **交差** (filter 間 AND) を
    # post-filter に使う。空集合が出れば交差も空 = 0 件 (vendor no-match / PIR 無 match を反映)。
    allowed_ids: set[str] | None = None
    if facets is not None and facets.entity_filters:
        try:
            sets = [repo.entity_article_ids(t, list(vs)) for t, vs in facets.entity_filters]
            allowed_ids = set.intersection(*sets) if sets else None
        except Exception as e:  # noqa: BLE001
            # facet lookup 失敗時は entity 絞り込みを諦めて degrade (silent drop は警告で記録)。
            _log.warning("retriever_entity_facet_failed", error=str(e))
            allowed_ids = None

    def _ingest(arts: list[ArticleRecord], via: str) -> list[str]:
        ids: list[str] = []
        for a in arts:
            by_id.setdefault(a.article_id, a)
            matched_via[a.article_id].add(via)
            ids.append(a.article_id)
        return ids

    # --- vector leg ---
    if embedder is not None and plan.semantic_query.strip():
        try:
            resp = await embedder.embed(plan.semantic_query, kind="query")
            hits = repo.find_similar_embeddings(
                list(resp.vector), model=embedder.model, top_k=top_k, threshold=0.0
            )
            arts_by_url = repo.get_articles_by_urls([u for _h, u, _s in hits])
            vlist: list[str] = []
            for _h, url, _sim in hits:
                a = arts_by_url.get(url)
                if a is not None:
                    by_id.setdefault(a.article_id, a)
                    matched_via[a.article_id].add("vector")
                    vlist.append(a.article_id)
            ranked_lists.append(vlist)
        except Exception as e:  # noqa: BLE001
            _log.warning("retriever_vector_failed", error=str(e))

    # --- keyword legs (各キーワードを 1 leg) ---
    for kw in plan.keywords[:_MAX_KEYWORDS]:
        term = kw.strip()
        if not term:
            continue
        try:
            arts = repo.list_articles(search=term, limit=_PER_LEG_LIMIT, **leg_kwargs)
            ranked_lists.append(_ingest(arts, "keyword"))
        except Exception as e:  # noqa: BLE001
            _log.warning("retriever_keyword_failed", term=term[:40], error=str(e))

    # --- structured leg: category ---
    cats = [c for c in plan.category_hints if c in _ALLOWED_CATEGORIES]
    if cats:
        try:
            arts = repo.list_articles(category_in=cats, status="posted", limit=_PER_LEG_LIMIT)
            ranked_lists.append(_ingest(arts, "structured"))
        except Exception as e:  # noqa: BLE001
            _log.warning("retriever_category_failed", error=str(e))

    # --- structured leg: intent ---
    if plan.intent_hint:
        try:
            arts = repo.list_articles(
                socio_political_intent=plan.intent_hint, status="posted", limit=_PER_LEG_LIMIT
            )
            ranked_lists.append(_ingest(arts, "structured"))
        except Exception as e:  # noqa: BLE001
            _log.warning("retriever_intent_failed", error=str(e))

    # --- structured leg: entity hints (actor / cve / ioc 等) ---
    for etype, evalue in plan.entity_hints[:_MAX_ENTITY_HINTS]:
        if etype not in _ALLOWED_ENTITY_TYPES or not evalue.strip():
            continue
        try:
            arts = repo.list_articles(
                entity_type=etype, entity_value=evalue.strip().lower(), limit=_PER_LEG_LIMIT
            )
            ranked_lists.append(_ingest(arts, "structured"))
        except Exception as e:  # noqa: BLE001
            _log.warning("retriever_entity_failed", etype=etype, error=str(e))

    if not ranked_lists:
        return []

    fused = _rrf(ranked_lists)
    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
    # facet で post-filter してから top_k を切る (top_k 後だと facet 適用で件数が痩せるため)。
    out: list[tuple[ArticleRecord, float, list[str]]] = []
    for aid, score in ordered:
        rec = by_id.get(aid)
        if rec is None:
            continue
        if facets is not None and not _facet_ok(rec, facets, allowed_ids):
            continue
        out.append((rec, score, sorted(matched_via[aid])))
        if len(out) >= top_k:
            break
    return out
