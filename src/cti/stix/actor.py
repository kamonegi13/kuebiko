"""アクター 1 件 → STIX 2.1 bundle (2026-09-27)。脅威アクター画面の「観測」を書き出す。

そのアクターが **主題** の記事 (直近 ``days`` 日、新しい順に最大 ``max_articles`` 件) を
記事の組み立て (article.build_article_bundle) に通し、report と関係を集めて
``grouping`` で束ねる。関係は記事ごと (主張した文脈ごと) のまま — 件数を確度に
読み替えない (収集量は重要性でも確かさでもない、CLAUDE.md §7)。

LLM medium の主題は外す (精度 61%、国家の集計と同じ扱い: subject_gate.trusted_subject_clause)。
⚠ 収集網の観測の記録であって、アクターの活動の全体像ではない (grouping の説明に明記する)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from src.cti.stix import objects as o
from src.cti.stix.article import build_article_bundle
from src.cti.stix.core import PRODUCER_ID, bundle, now_ts, sdo, with_extension
from src.cti.stix.facts import facts_from_db

if TYPE_CHECKING:
    from src.cti.actor_normalizer import ActorAliasRegistry
    from src.storage.run_history import RunHistoryRepository

DEFAULT_DAYS = 180
DEFAULT_MAX_ARTICLES = 50
_HEADER_TYPES = frozenset({"marking-definition", "extension-definition"})
OBSERVATION_NOTE = (
    "収集網が観測した、このアクターが主題の記事の記録。アクターの活動の全体像ではない。"
)


def _subject_article_ids(
    repo: RunHistoryRepository, ids: set[str], *, days: int, limit: int
) -> list[str]:
    now = datetime.now(UTC)
    rows = repo.list_subject_article_rows(now - timedelta(days=days), now + timedelta(minutes=1))
    hits: dict[str, str] = {}
    for r in rows:
        # ⚠ 上限で切り詰める **前に** 信頼できない主題を外す (後から外すと、LLM medium が
        #   直近に多いアクターで信頼できる記事が上限の外へ押し出される)
        if r.get("subject_actor_source") == "llm" and r.get("subject_actor_confidence") != "high":
            continue
        subjects = {s.strip() for s in str(r["subject_actor_ids"]).split(",")}
        if ids & subjects:
            hits[str(r["article_id"])] = max(hits.get(str(r["article_id"]), ""), r["created_at"])
    return [a for a, _ in sorted(hits.items(), key=lambda kv: kv[1], reverse=True)][:limit]


def build_actor_bundle(
    repo: RunHistoryRepository,
    registry: ActorAliasRegistry,
    actor_id: str,
    *,
    days: int = DEFAULT_DAYS,
    max_articles: int = DEFAULT_MAX_ARTICLES,
) -> dict[str, Any] | None:
    """アクター 1 件の STIX 2.1 bundle (辞書に無ければ None)。"""
    canonical = registry.resolve_actor_id(actor_id)
    actor = registry.by_id(canonical)
    if actor is None:
        return None
    ids = {canonical, *registry.merged_sources(canonical)}
    objs: list[dict[str, Any]] = [o.actor_object(actor)]
    report_ids: list[str] = []
    for aid in _subject_article_ids(repo, ids, days=days, limit=max_articles):
        facts = facts_from_db(repo, aid)
        if facts is None:
            continue
        for x in build_article_bundle(facts, registry)["objects"]:
            if x["type"] in _HEADER_TYPES or x["id"] == PRODUCER_ID:
                continue
            objs.append(x)
            if x["type"] == "report":
                report_ids.append(x["id"])
    ts = now_ts()
    grouping = with_extension(
        sdo(
            "grouping",
            f"actor-observation|{canonical}|{days}",
            _ts=ts,
            name=f"{actor.canonical} の観測 (直近 {days} 日)",
            description=OBSERVATION_NOTE,
            context="suspicious-activity",
            object_refs=[o.actor_ref(actor), *report_ids],
        ),
        {"actor_id": canonical, "actor_kind": actor.kind},
    )
    return bundle([grouping, *objs], key=f"actor|{canonical}|{days}")
