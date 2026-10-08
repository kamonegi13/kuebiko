"""線でたどった関連事象の節を DB から組む — GraphRAG 共通の取得 (2026-10-08)。

``src/spotlight/graph_context.py`` (Spotlight 専用、2026-09-29) を一般化し、状況総括・
事象ニュース・PIR 別の要点など、どの生成経路からも呼べるようにしたもの。

- ``article_ids`` を渡すと記事 → 構成する事象へ畳んでから線をたどる (Spotlight の形)
- ``event_ids`` を渡すと事象をそのまま起点にする (状況総括・PIR 別の要点向け)

線の付帯情報 (相手事象の種別・被害業種/国・要点 1 文・共有指標の珍しさ) は
``src/eventnews/relation_features.py`` の ``EventFeatures`` と、最新版の ``bluf`` から組む。
節に出すのは ``relations.ENABLED_TYPES`` (続報・側面・包含・同じ出来事の関連・同じアクター) と、
GraphRAG 専用の線のうち精度を測って開いた ``relations.GRAPHRAG_ENABLED_EXTRA`` (同じ帰属国、
2026-10-08)。``include_extra=False`` で前者だけに戻せる。
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from src.graph.render import MAX_LINES, EventContext, PriorityHint, render_relation_section

#: 関係を引く既定の期間 (日)。呼び手の窓より長く、窓の前の経緯まで届くように
RELATION_DAYS = 120
#: 共有指標の珍しさ (df) を表示する際の母集団窓。eventnews.relations.WINDOW_DAYS と揃える
RARITY_WINDOW_DAYS = 60
_RARITY_ATTRS: tuple[tuple[str, str], ...] = (
    ("cves", "cve"),
    ("malware", "cap"),
    ("tools", "cap"),
    ("victims", "victim"),
)


def _event_summary(body_json: str) -> str:
    """最新版の BLUF (先頭の要点) — 相手事象の要点 1 文に使う。"""
    if not body_json:
        return ""
    try:
        body = json.loads(body_json)
    except (TypeError, ValueError):
        return ""
    bluf = body.get("bluf")
    return str(bluf).strip() if isinstance(bluf, str) else ""


def _members_from_articles(repo: Any, article_ids: Sequence[str]) -> dict[str, list[str]]:
    marks = ",".join("?" for _ in article_ids)
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        rows = conn.execute(
            "SELECT m.article_id, m.item_id FROM event_item_members m "
            "JOIN event_items i ON i.id = m.item_id "
            f"WHERE m.article_id IN ({marks}) AND i.merged_into IS NULL",
            tuple(article_ids),
        ).fetchall()
    members: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        members[str(r[0])].append(str(r[1]))
    return dict(members)


def _first_reported(repo: Any, item_ids: set[str]) -> dict[str, datetime]:
    if not item_ids:
        return {}
    marks = ",".join("?" for _ in item_ids)
    out: dict[str, datetime] = {}
    with repo._connect() as conn:  # noqa: SLF001
        for row in conn.execute(
            f"SELECT id, first_reported_at FROM event_items WHERE id IN ({marks})",  # noqa: S608
            tuple(item_ids),
        ).fetchall():
            out[str(row[0])] = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
    return out


def _member_fallback(repo: Any, item_ids: set[str]) -> dict[str, tuple[str, str]]:
    """要約版の無い事象の見出し・要点 — 最初に報じた構成記事の見出しと要約で代用する。

    事象の大半 (2026-10-08 実測で直近 120 日の 87%) は単独記事で要約版を持たない。
    版の見出しだけを使うと、その事象への線が節から落ちる。
    """
    if not item_ids:
        return {}
    marks = ",".join("?" for _ in item_ids)
    out: dict[str, tuple[str, str]] = {}
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        rows = conn.execute(
            "SELECT m.item_id, a.title, a.summary, a.published_at FROM event_item_members m "
            "JOIN articles a ON a.article_id = m.article_id "
            f"WHERE m.item_id IN ({marks}) ORDER BY m.item_id, a.published_at, a.id",  # noqa: S608
            tuple(item_ids),
        ).fetchall()
    for r in rows:
        iid = str(r[0])
        if iid in out or not r[1]:
            continue
        out[iid] = (str(r[1]).strip(), str(r[2] or "").strip())
    return out


def build_relation_context(
    repo: Any,
    *,
    article_ids: Sequence[str] = (),
    event_ids: Sequence[str] = (),
    window_end: datetime,
    relation_days: int = RELATION_DAYS,
    rarity_window_days: int = RARITY_WINDOW_DAYS,
    priority: PriorityHint | None = None,
    max_lines: int = MAX_LINES,
    include_extra: bool = True,
) -> str:
    """DB から線・事象の付帯情報を読んで節を組む (失敗は呼び手が握る)。

    ``article_ids`` か ``event_ids`` のどちらかを渡す (両方空なら空文字)。
    """
    from src.cti.actor_normalizer import load_actor_aliases
    from src.eventnews.relation_features import load_event_features
    from src.eventnews.relations import _df, graphrag_extra_by_event, relations_by_event

    ids = list(article_ids) if article_ids else list(event_ids)
    if not ids:
        return ""

    members = (
        _members_from_articles(repo, article_ids)
        if article_ids
        else {eid: [eid] for eid in event_ids}
    )
    own_events = {ev for evs in members.values() for ev in evs}
    if not own_events:
        return ""

    base = relations_by_event(repo, days=relation_days)
    extra = graphrag_extra_by_event(repo, days=relation_days) if include_extra else {}
    # キャッシュ済みの dict を書き換えないよう、起点の事象の分だけ新しく組む
    relations = {ev: [*base.get(ev, ()), *extra.get(ev, ())] for ev in own_events}
    others = {
        (rel.b if rel.a == ev else rel.a) for ev in own_events for rel in relations.get(ev, ())
    }
    first = _first_reported(repo, own_events | others)
    versions = repo.latest_event_versions(list(others)) if others else {}
    headlines = {k: str(v.headline) for k, v in versions.items() if v.headline}
    fallback = _member_fallback(repo, others - set(headlines))
    headlines.update({k: title for k, (title, _) in fallback.items()})

    aliases = load_actor_aliases()

    def actor_name(actor_id: str) -> str:
        actor = aliases.by_id(actor_id)
        return actor.canonical if actor is not None else actor_id

    features = {f.item_id: f for f in load_event_features(repo, days=relation_days)}
    event_context: dict[str, EventContext] = {}
    for oid in others:
        feat = features.get(oid)
        ver = versions.get(oid)
        if feat is None and ver is None and oid not in fallback:
            continue
        summary = _event_summary(ver.body_json) if ver else ""
        event_context[oid] = EventContext(
            kind=next(iter(feat.kinds), "") if feat else "",
            sector=next(iter(feat.sectors), "") if feat else "",
            country=next(iter(feat.countries), "") if feat else "",
            summary=summary or fallback.get(oid, ("", ""))[1],
        )

    indicator_rarity: dict[str, int] = {}
    all_events = list(features.values())
    for attr, prefix in _RARITY_ATTRS:
        for value, count in _df(all_events, attr).items():
            indicator_rarity[f"{prefix}:{value}"] = count

    return render_relation_section(
        ids,
        members=members,
        relations=relations,
        first_reported=first,
        headlines=headlines,
        window_end=window_end,
        actor_name=actor_name,
        max_lines=max_lines,
        event_context=event_context,
        indicator_rarity=indicator_rarity,
        rarity_window_days=rarity_window_days,
        priority=priority,
    )


__all__ = ["RELATION_DAYS", "build_relation_context"]
