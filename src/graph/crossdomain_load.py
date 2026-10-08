"""領域をまたぐ線の入力 (CrossEvent) を DB から組み立てる (読み取り専用、試作 2026-10-09)。

事象の領域は構成記事のカテゴリの多数決: 地政学・政策が過半なら geo、それ以外で
国家系アクターを主題に持つものは cyber。どちらでもない事象は線の対象外。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from src.graph.crossdomain import CrossEvent

if TYPE_CHECKING:
    from src.storage.run_history import RunHistoryRepository

GEO_CATEGORIES = frozenset({"geopolitical", "policy"})
_CHUNK = 800


def _ts(value: object) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _fetch(
    repo: RunHistoryRepository, since: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の集約
        members = [
            dict(r)
            for r in conn.execute(
                "SELECT m.item_id, m.article_id, i.first_reported_at"
                " FROM event_item_members m JOIN event_items i ON i.id = m.item_id"
                " WHERE (i.merged_into IS NULL OR i.merged_into = '') AND i.last_reported_at >= ?",
                (since.isoformat(),),
            ).fetchall()
        ]
        aids = sorted({str(m["article_id"]) for m in members})
        arts: list[dict[str, Any]] = []
        ents: list[dict[str, Any]] = []
        for i in range(0, len(aids), _CHUNK):
            chunk = aids[i : i + _CHUNK]
            ph = ",".join("?" for _ in chunk)
            arts += [
                dict(r)
                for r in conn.execute(
                    "SELECT article_id, category, socio_political_intent, victim_country_iso,"  # noqa: S608
                    " subject_actor_ids, subject_actor_source, subject_actor_confidence"
                    f" FROM articles WHERE article_id IN ({ph})",
                    chunk,
                ).fetchall()
            ]
            ents += [
                dict(r)
                for r in conn.execute(
                    "SELECT article_id, value FROM article_entities"  # noqa: S608
                    f" WHERE entity_type = 'involved_country' AND article_id IN ({ph})",
                    chunk,
                ).fetchall()
            ]
    return members, arts, ents


def load_cross_events(
    repo: RunHistoryRepository, *, days: int = 90, now: datetime | None = None
) -> list[CrossEvent]:
    from src.assessment.situation_track import registry_is_group
    from src.cti.actor_normalizer import load_actor_aliases
    from src.cti.threat_actor_doctrine import state_nation_map
    from src.eventnews.relation_features import _trusted_subjects

    since = (now or datetime.now(UTC)) - timedelta(days=days)
    members, arts, ents = _fetch(repo, since)
    nation_of = state_nation_map(load_actor_aliases().actors)
    is_group = registry_is_group()
    art = {str(a["article_id"]): a for a in arts}
    involved_by_art: dict[str, set[str]] = defaultdict(set)
    for e in ents:
        involved_by_art[str(e["article_id"])].add(str(e["value"]).upper())

    firsts: dict[str, datetime] = {}
    by_item: dict[str, list[str]] = defaultdict(list)
    for m in members:
        iid = str(m["item_id"])
        firsts[iid] = _ts(m["first_reported_at"])
        by_item[iid].append(str(m["article_id"]))
    headlines = repo.latest_event_versions(list(by_item))

    out: list[CrossEvent] = []
    for iid, aids in by_item.items():
        rows = [art[a] for a in aids if a in art]
        cats = Counter(str(r.get("category") or "") for r in rows)
        geo_n = sum(cats[c] for c in GEO_CATEGORIES)
        headline = headlines[iid].headline if iid in headlines else ""
        if rows and geo_n * 2 > len(rows):
            involved = {c for a in aids for c in involved_by_art.get(a, ())}
            out.append(
                CrossEvent(iid, firsts[iid], "geo", involved=frozenset(involved), headline=headline)
            )
            continue
        subjects = {s for r in rows for s in _trusted_subjects(r, is_group)}
        nations = {nation_of[s].upper() for s in subjects if s in nation_of}
        if not nations:
            continue
        victims = {
            str(r["victim_country_iso"]).upper() for r in rows if r.get("victim_country_iso")
        }
        intents = {
            str(r["socio_political_intent"]) for r in rows if r.get("socio_political_intent")
        }
        out.append(
            CrossEvent(
                iid,
                firsts[iid],
                "cyber",
                actor_nations=frozenset(nations),
                victim_countries=frozenset(victims),
                intents=frozenset(intents),
                headline=headline,
            )
        )
    return out
