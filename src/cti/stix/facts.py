"""STIX にする記事の事実 (ArticleFacts) を、DB の行 / 配信メッセージから作る (2026-09-27)。

- ``facts_from_db``: 記事ページの「STIX を書き出す」(保存済みの entity・主題判定を使う)
- ``facts_from_briefing``: Discord 投稿への添付 (取込の途中、メッセージの metadata を使う)

IOC は保存済みの entity に加えて、見出し・本文を extract_iocs で読み直す (取込時と同じ関門)。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from src.cti.ioc_extractor import ExtractedIocs, extract_iocs
from src.cti.stix.article import ArticleFacts
from src.cti.stix.core import to_ts

if TYPE_CHECKING:
    from src.cti.actor_normalizer import ActorAliasRegistry
    from src.storage.run_history import RunHistoryRepository
    from src.tools.discord_publisher import BriefingMessage

#: ExtractedIocs の欄 → objects._IOC_PATTERN の種別
_IOC_FIELDS = (
    ("ipv4", "ipv4"),
    ("ipv6", "ipv6"),
    ("domains", "domain"),
    ("urls", "url"),
    ("md5", "md5"),
    ("sha1", "sha1"),
    ("sha256", "sha256"),
)


def iocs_of(extracted: ExtractedIocs) -> tuple[tuple[str, str], ...]:
    """ExtractedIocs → ((種別, 値), ...)。重複は落とす。"""
    out: dict[tuple[str, str], None] = {}
    for attr, kind in _IOC_FIELDS:
        for v in getattr(extracted, attr):
            out[(kind, str(v))] = None
    return tuple(out)


def _split(csv: str | None) -> tuple[str, ...]:
    return tuple(s.strip() for s in (csv or "").split(",") if s.strip())


def _values(ents: Mapping[str, Mapping[str, int]], entity_type: str) -> tuple[str, ...]:
    return tuple(sorted(ents.get(entity_type, {})))


def facts_from_db(repo: RunHistoryRepository, article_id: str) -> ArticleFacts | None:
    """保存済みの記事と entity から ArticleFacts を作る (記事が無ければ None)。"""
    a = repo.get_article(article_id)
    if a is None:
        return None
    ents = repo.count_entities_for_articles([article_id])
    body = repo.get_article_body(article_id) or ""
    # 保存済みの IOC も同じ抽出に通す (本文を捨てた古い記事でも entity から書き出せる)
    ioc_values = [v for t, vals in ents.items() if t.startswith("ioc_") for v in vals]
    extracted = extract_iocs("\n".join([a.title or "", body, *ioc_values]))
    ttps = sorted({*_values(ents, "ttp"), *extracted.mitre_techniques})
    cves = sorted({*_values(ents, "cve"), *extracted.cves})
    return ArticleFacts(
        article_id=article_id,
        title=a.title or "",
        url=a.url or "",
        feed_title=a.feed_title or "",
        summary=a.summary or "",
        created=to_ts(a.created_at),
        published=to_ts(a.published_at),
        importance=a.importance or "",
        category=a.category or "",
        pir_ids=_values(ents, "pir"),
        subject_actor_ids=_split(a.subject_actor_ids),
        subject_source=a.subject_actor_source or "",
        subject_confidence=a.subject_actor_confidence or "",
        mentioned_actor_ids=_values(ents, "actor"),
        malware=_values(ents, "malware_family"),
        tools=_values(ents, "tool"),
        ttps=tuple(ttps),
        cves=tuple(cves),
        iocs=iocs_of(extracted),
        victim_orgs=_values(ents, "victim_org"),
        victim_country_iso=(a.victim_country_iso or "").strip(),
        victim_sector=a.victim_sector_canonical or "",
        intent=a.socio_political_intent or "",
        intent_confidence=a.intent_confidence or "",
    )


def _meta_str(meta: Mapping[str, Any], key: str) -> str:
    v = meta.get(key)
    return v if isinstance(v, str) else ""


def _meta_list(meta: Mapping[str, Any], key: str) -> tuple[str, ...]:
    v = meta.get(key)
    if isinstance(v, str):
        return _split(v)
    if isinstance(v, (list, tuple)):
        return tuple(str(x) for x in v if str(x).strip())
    return ()


def _subject_of(
    msg: BriefingMessage, mentioned: tuple[str, ...], registry: ActorAliasRegistry
) -> tuple[tuple[str, ...], str, str]:
    """投稿の時点ではまだ保存されていない主題を、保存時と同じ判定点で求める。"""
    from src.cti.subject_actor import determine_subject_actors

    meta = msg.metadata
    rf_raw = meta.get("routing_flags")
    rf = rf_raw if isinstance(rf_raw, dict) else {}
    subj = determine_subject_actors(
        titles=(msg.title or "", _meta_str(meta, "original_title")),
        detected_actor_ids=mentioned,
        llm_primary_actor_id=str(rf.get("primary_actor_id") or ""),
        llm_confidence=str(rf.get("confidence") or "low"),
        category=msg.category,
        registry=registry,
    )
    return subj.ids, subj.source, subj.confidence or ""


def facts_from_briefing(
    msg: BriefingMessage,
    extracted: ExtractedIocs,
    *,
    article_id: str,
    registry: ActorAliasRegistry,
    mentioned_actor_ids: tuple[str, ...] | None = None,
) -> ArticleFacts:
    """配信メッセージ (取込の途中) から ArticleFacts を作る。

    ``mentioned_actor_ids`` を渡さなければ metadata の detected_actor_ids を言及とする。
    """
    meta = msg.metadata
    source = msg.sources[0] if msg.sources else None
    mentioned = (
        mentioned_actor_ids
        if mentioned_actor_ids is not None
        else _meta_list(meta, "detected_actor_ids")
    )
    subject_ids, subject_source, subject_conf = _subject_of(msg, mentioned, registry)
    return ArticleFacts(
        article_id=article_id,
        title=msg.title,
        url=str(source.url) if source is not None else "",
        feed_title=str(source.title) if source is not None else "",
        summary=msg.summary or msg.bluf or "",
        importance=str(msg.importance or ""),
        category=str(msg.category or ""),
        pir_ids=_meta_list(meta, "pir_ids"),
        subject_actor_ids=subject_ids,
        subject_source=subject_source,
        subject_confidence=subject_conf,
        mentioned_actor_ids=mentioned,
        malware=_meta_list(meta, "malware_families"),
        ttps=tuple(extracted.mitre_techniques),
        cves=tuple(extracted.cves),
        iocs=iocs_of(extracted),
        victim_country_iso=_meta_str(meta, "victim_country_iso"),
        victim_sector=_meta_str(meta, "victim_sector_canonical"),
        intent=_meta_str(meta, "socio_political_intent"),
        intent_confidence=_meta_str(meta, "intent_confidence"),
    )
