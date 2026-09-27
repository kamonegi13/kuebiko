"""記事 1 件 → STIX 2.1 bundle (report が中心、2026-09-27)。

⭐ 関係 (uses / targets / indicates / attributed-to) は **主題アクター** にだけ張る。
言及だけのアクター (比較で名前が出ただけ等) は report の object_refs に載せるが、
手口や被害と結ばない — 言及を帰属として出すと受け手の知識グラフを汚す
(kuebiko 自身が 2026-07-17 に主題と言及を分けた理由と同じ)。

関係の confidence は主題の判定経路から (フィード・見出しの別名 = high、LLM = その確度)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
from src.cti.diamond_model import intent_to_stix_motivation
from src.cti.stix import objects as o
from src.cti.stix.core import bundle, confidence_value, now_ts, sdo, with_extension

#: 主題の判定経路 → 確度 (LLM 経路は記事ごとの確度を使う)
_SOURCE_CONFIDENCE = {"feed": "high", "feed_match": "high", "title": "high"}
_VULN_CATEGORIES = frozenset({"vulnerability"})


@dataclass(frozen=True)
class ArticleFacts:
    """STIX にする記事の事実 (DB の行 / 配信メッセージのどちらからでも作る)。"""

    article_id: str
    title: str
    url: str = ""
    feed_title: str = ""
    summary: str = ""
    created: str | None = None
    published: str | None = None
    importance: str = ""
    category: str = ""
    pir_ids: tuple[str, ...] = ()
    subject_actor_ids: tuple[str, ...] = ()
    subject_source: str = ""
    subject_confidence: str = ""
    mentioned_actor_ids: tuple[str, ...] = ()
    malware: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    ttps: tuple[str, ...] = ()
    cves: tuple[str, ...] = ()
    iocs: tuple[tuple[str, str], ...] = ()  # (種別, 値)。種別は objects._IOC_PATTERN のキー
    victim_orgs: tuple[str, ...] = ()
    victim_country_iso: str = ""
    victim_sector: str = ""
    intent: str = ""
    intent_confidence: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _subject_confidence(f: ArticleFacts) -> int | None:
    level = _SOURCE_CONFIDENCE.get(f.subject_source) or f.subject_confidence
    return confidence_value(level)


def _resolve(registry: ActorAliasRegistry, ids: Sequence[str]) -> list[ActorAlias]:
    out: list[ActorAlias] = []
    seen: set[str] = set()
    for raw in ids:
        actor = registry.by_id(registry.resolve_actor_id(raw.strip()))
        if actor is not None and actor.id not in seen:
            seen.add(actor.id)
            out.append(actor)
    return out


def _entity_objects(f: ArticleFacts, valid_from: str) -> dict[str, list[dict[str, Any]]]:
    sector = o.sector_stix(f.victim_sector) if f.victim_sector else None
    victims = [
        o.victim_org_object(n, sectors=[sector[1]] if sector else None) for n in f.victim_orgs
    ]
    indicators = [x for k, v in f.iocs if (x := o.indicator_object(k, v, valid_from=valid_from))]
    return {
        "uses": [
            *(o.malware_object(n) for n in f.malware),
            *(o.tool_object(n) for n in f.tools),
            *(o.attack_pattern_object(t) for t in f.ttps),
        ],
        "targets": [
            *(o.vulnerability_object(c) for c in f.cves),
            *victims,
            *([o.location_object(f.victim_country_iso)] if f.victim_country_iso else []),
            *([o.sector_object(*sector)] if sector else []),
        ],
        "indicators": indicators,
    }


def _subject_relationships(
    subjects: list[ActorAlias],
    ents: dict[str, list[dict[str, Any]]],
    registry: ActorAliasRegistry,
    confidence: int | None,
    *,
    context: str,
    created: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(主題アクターから張る関係, 関係のために足す上位組織)。"""

    def rel(src: str, kind: str, tgt: str, conf: int | None = confidence) -> dict[str, Any]:
        return o.relationship(src, kind, tgt, context=context, created=created, confidence=conf)

    rels: list[dict[str, Any]] = []
    sponsors: list[dict[str, Any]] = []
    groups = [a for a in subjects if not o.is_org(a)]
    for g in groups:
        ref = o.actor_ref(g)
        rels += [rel(ref, "uses", x["id"]) for x in ents["uses"]]
        rels += [rel(ref, "targets", x["id"]) for x in ents["targets"]]
        sponsor = registry.by_id(g.sponsor_org) if g.sponsor_org else None
        if sponsor is not None:
            sponsors.append(o.actor_object(sponsor))
            # 上位組織への帰属は辞書の知識 (記事の主張ではない) — 確度は付けない
            rels.append(rel(ref, "attributed-to", o.actor_ref(sponsor), None))
    if len(groups) == 1:
        # 指標が指す主体は主題アクターが 1 つに定まるときだけ (複数なら誰の指標か分からない)
        ref = o.actor_ref(groups[0])
        rels += [rel(x["id"], "indicates", ref) for x in ents["indicators"]]
    return rels, sponsors


def _report(f: ArticleFacts, refs: list[str], created: str) -> dict[str, Any]:
    rep = sdo(
        "report",
        f"article|{f.article_id}",
        _ts=created,
        name=f.title or f.article_id,
        description=f.summary or None,
        published=f.published or created,
        report_types=["vulnerability"] if f.category in _VULN_CATEGORIES else ["threat-report"],
        object_refs=refs,
        external_references=[{"source_name": f.feed_title or "source", "url": f.url}]
        if f.url
        else None,
    )
    return with_extension(
        rep,
        {
            "article_id": f.article_id,
            "importance": f.importance,
            "category": f.category,
            "pir_ids": list(f.pir_ids),
            "subject_actor_ids": list(f.subject_actor_ids),
            "subject_source": f.subject_source,
            "subject_confidence": f.subject_confidence,
            "mentioned_actor_ids": list(f.mentioned_actor_ids),
            "intent": f.intent,
            "intent_confidence": f.intent_confidence,
            **f.extra,
        },
    )


def build_article_bundle(f: ArticleFacts, registry: ActorAliasRegistry) -> dict[str, Any]:
    """記事 1 件の STIX 2.1 bundle。"""
    created = f.created or now_ts()
    motivation = intent_to_stix_motivation(f.intent) if f.intent else None
    subjects = _resolve(registry, f.subject_actor_ids)
    subject_ids = {a.id for a in subjects}
    mentioned = [a for a in _resolve(registry, f.mentioned_actor_ids) if a.id not in subject_ids]
    actor_objs = [
        *(o.actor_object(a, primary_motivation=motivation) for a in subjects),
        *(o.actor_object(a) for a in mentioned),
    ]
    ents = _entity_objects(f, valid_from=f.published or created)
    rels, sponsors = _subject_relationships(
        subjects,
        ents,
        registry,
        _subject_confidence(f),
        context=f"article|{f.article_id}",
        created=created,
    )
    body = [
        *actor_objs,
        *sponsors,
        *ents["uses"],
        *ents["targets"],
        *ents["indicators"],
        *rels,
    ]
    refs = list(dict.fromkeys(x["id"] for x in body))
    if not refs:
        # report の object_refs は 1 件以上が必須 — 何も無い記事は作成者を指す
        from src.cti.stix.core import PRODUCER_ID

        refs = [PRODUCER_ID]
    return bundle([_report(f, refs, created), *body], key=f"article|{f.article_id}")
