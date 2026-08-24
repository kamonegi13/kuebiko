"""事象単位ニュースの read-only API (Tier0 = 匿名で閲覧可)。

設計 SSoT: docs/event_news_design.md。読み手向けの唯一の出口で、以下を守る:

- **生成物と原ソースを構造で区別する** (§3)。本文は「kuebiko が生成」であることを
  レスポンスで明示し、構成記事は**必ず全件**返す (省略しない)。事実行は
  ``source_index`` を持ち、フロントが文末の出典番号として描く
- **裏取りは「独立媒体数 × tier」で返す** (§3-3)。記事数だけを裏取りとして出さない。
  国営・未分類は 3 値で別に返し、**未分類を 0 と見せない**
- GET のみ。readonly instance でも追加ガードなしで動く (Tier0)
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, HTTPException

from src.cti.source_basis import classify_source_tier
from src.storage.run_history import RunHistoryRepository
from src.ui.api.articles_feed import RELATED_ENTITY_TYPE_ORDER

eventnews_api = APIRouter(prefix="/api/v1/eventnews", tags=["eventnews"])

# 読み手に「これは生成物である」ことを常に伝える注記 (UI がそのまま表示する)
GENERATED_NOTE = "kuebiko が複数媒体の記事から生成した要約であり、原記事そのものではない"

_LIST_LIMIT_MAX = 200


def _repo() -> RunHistoryRepository:
    return RunHistoryRepository()


def _version_payload(repo: RunHistoryRepository, item_id: str) -> dict[str, Any] | None:
    versions = repo.list_event_versions(item_id)
    if not versions:
        return None
    latest = versions[0]  # version DESC で返るので先頭が最新
    body = json.loads(latest.body_json) if latest.body_json else {}
    return {
        "version": latest.version,
        "generated_at": latest.generated_at.isoformat(),
        "model": latest.model,
        "headline": latest.headline,
        "bluf": body.get("bluf", ""),
        "facts": body.get("facts", []),
        "discrepancies": body.get("discrepancies", []),
        "unknowns": body.get("unknowns", []),
        # 関門が黙って落とした量を読み手にも見せる (落下率の常設監視、§9)
        "dropped_lines": latest.dropped_lines,
        "resolved_ids": latest.repaired_ids,
        "history": [
            {"version": v.version, "generated_at": v.generated_at.isoformat()} for v in versions
        ],
    }


def _headlines_and_previews(
    repo: RunHistoryRepository, records: Sequence[Any]
) -> dict[str, tuple[str, str]]:
    """一覧の見出しと冒頭を **一括** で解決する。

    アイテムごとに版と記事を引くと N+1 になる (1 日 ~127 件のペースで事象が増えるため、
    数日で一覧が目に見えて遅くなる)。版は ``latest_event_versions``、記事は
    ``get_articles_by_ids`` で **それぞれ 1 クエリ**にまとめる。
    """
    versions = repo.latest_event_versions([r.state.item_id for r in records])
    need_article = [r for r in records if r.state.item_id not in versions]
    articles = repo.get_articles_by_ids([aid for r in need_article for aid in r.state.member_ids])
    out: dict[str, tuple[str, str]] = {}
    for r in records:
        latest = versions.get(r.state.item_id)
        if latest is not None:
            body = json.loads(latest.body_json) if latest.body_json else {}
            out[r.state.item_id] = (latest.headline, str(body.get("bluf", ""))[:160])
            continue
        for aid in r.state.member_ids:
            art = articles.get(aid)
            if art is not None:
                out[r.state.item_id] = (art.title, (art.summary or "")[:160])
                break
        else:
            out[r.state.item_id] = ("(記事の取得に失敗)", "")
    return out


def _members_payload(repo: RunHistoryRepository, item_id: str) -> list[dict[str, Any]]:
    """構成記事を **全件** 返す (§3-1: 折りたたみは可・省略は不可)。"""
    members = repo.list_event_members(item_id)
    articles = repo.get_articles_by_ids([m.article_id for m in members])
    out: list[dict[str, Any]] = []
    for i, m in enumerate(members, start=1):
        art = articles.get(m.article_id)
        feed_title = art.feed_title if art else ""
        feed_url = getattr(art, "feed_url", "") or "" if art else ""
        out.append(
            {
                "index": i,
                "article_id": m.article_id,
                "title": art.title if art else "",
                "url": art.url if art else "",
                "feed_title": feed_title,
                "source_tier": classify_source_tier(feed_title or "", feed_url),
                "published_at": art.published_at if art else None,
                "summary": (art.summary if art else "") or "",
                "joined_at": m.joined_at.isoformat(),
                "contributed_new_facts": bool(m.contributed_new_facts),
            }
        )
    return out


# メタデータの 1 種別あたり表示上限。ttp / ioc は 1 事象で数十件になりうるため、
# 全件返すと読み手が本文に辿り着けない。省いた数は必ず返す (黙って切らない)。
_METADATA_VALUE_CAP = 24


def _metadata_payload(repo: RunHistoryRepository, member_ids: Sequence[str]) -> dict[str, Any]:
    """構成記事から **決定論で** 集約したメタデータ。

    生成本文とは別物として返す — ここは LLM を通らないので、値の正しさは抽出層の
    品質そのもの。事象ニュースは「何が起きたか」の散文だが、実務では
    「どの CVE か・どのアクターか・どの手口か」が判断材料になる。原記事を 1 件ずつ
    開かないと分からない状態を解消するのがこの節の目的。

    ラベルは付けない (値のみ返す) — 表示名の SSoT は frontend の ``vocabLabel``
    (backend 配信の語彙) 一つに保つ。
    """
    ids = list(member_ids)
    if not ids:
        return {"entities": [], "subject_actors": [], "facets": []}

    raw = repo.count_entities_for_articles(ids)
    ordered = [*RELATED_ENTITY_TYPE_ORDER, *sorted(set(raw) - set(RELATED_ENTITY_TYPE_ORDER))]
    groups: list[dict[str, Any]] = []
    for etype in ordered:
        values = raw.get(etype)
        if not values:
            continue
        ranked = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
        entry: dict[str, Any] = {
            "type": etype,
            "values": [{"value": v, "articles": n} for v, n in ranked[:_METADATA_VALUE_CAP]],
            "omitted": max(0, len(ranked) - _METADATA_VALUE_CAP),
        }
        if etype == "cve":
            from src.tools.nvd_client import get_cvss

            scores = {}
            for v, _ in ranked[:_METADATA_VALUE_CAP]:
                info = get_cvss(v)
                if info:
                    scores[v] = {"score": info[0], "severity": info[1]}
            if scores:
                entry["cvss"] = scores
        groups.append(entry)

    articles = repo.get_articles_by_ids(ids)
    subject_counts: dict[str, int] = {}
    facet_counts: dict[str, dict[str, int]] = {}
    for art in articles.values():
        for sid in (art.subject_actor_ids or "").split(","):
            if sid.strip():
                subject_counts[sid.strip()] = subject_counts.get(sid.strip(), 0) + 1
        for key, value in (
            ("victim_sector", art.victim_sector_canonical),
            ("victim_country", art.victim_country_iso),
            ("socio_political_intent", art.socio_political_intent),
            ("category", art.category),
        ):
            if value:
                # ⚠ 代入文は右辺が先に評価される。``setdefault(...)[v] = facet_counts[key]...``
                # と 1 行で書くと右辺の facet_counts[key] が KeyError になる。
                bucket = facet_counts.setdefault(key, {})
                bucket[value] = bucket.get(value, 0) + 1

    from src.cti.actor_normalizer import load_actor_aliases

    registry = load_actor_aliases()
    subject_actors = []
    for sid, n in sorted(subject_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        entry_ = registry.by_id(registry.resolve_actor_id(sid))
        subject_actors.append(
            {"id": sid, "label": entry_.canonical if entry_ else sid, "articles": n}
        )

    facets = [
        {
            "key": key,
            "values": [
                {"value": v, "articles": n}
                for v, n in sorted(vals.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        }
        for key, vals in facet_counts.items()
    ]
    return {"entities": groups, "subject_actors": subject_actors, "facets": facets}


@eventnews_api.get("")
def list_event_news(
    limit: int = 50, status: str | None = None, importance: str | None = None
) -> dict[str, Any]:
    """事象一覧 (新着順)。origin='live' のみ — リプレイ行は返さない。

    **単独記事も返す** (docs/event_news_design.md §14b 案 A)。生成ニュースを持つのは
    複数媒体の事象だけだが、単独記事は原記事の見出し・要約をそのまま同じ枠で読ませる。
    ここを複数媒体に限ると読み手は記事一覧と 2 箇所を読むことになり、事象単位化の
    目的 (読む場所を 1 つにする) を果たさない。
    """
    repo = _repo()
    statuses = [s.strip() for s in status.split(",")] if status else None
    wanted = [i.strip() for i in importance.split(",")] if importance else None
    # 絞り込みは **LIMIT より前** に効かせる。取得後に filter すると「新着 N 件のうち
    # high のもの」になり、「high の新着 N 件」にならない (遡及構築で 2,000 件規模に
    # なって顕在化: high 絞り込みが数件しか出なくなる)。
    shown = repo.list_event_items(
        origin="live",
        statuses=statuses,
        importances=wanted,
        exclude_merged=True,
        limit=min(limit, _LIST_LIMIT_MAX),
    )
    resolved = _headlines_and_previews(repo, shown)
    items = []
    for r in shown:
        headline, preview = resolved[r.state.item_id]
        items.append(
            {
                "id": r.state.item_id,
                "headline": headline,
                "preview": preview,
                "status": r.state.status,
                "change_kind": r.change_kind,
                "importance": r.state.importance,
                "member_count": len(r.state.member_ids),
                # 裏取りは 3 値で返す。member_count を裏取りとして使わせない
                "independent_sources": r.independent_sources,
                "state_media_count": r.state_media_count,
                "unclassified_sources": r.unclassified_sources,
                "best_source_tier": r.best_source_tier,
                "first_reported_at": r.state.first_reported_at.isoformat(),
                "last_reported_at": r.state.last_reported_at.isoformat(),
                "current_version": r.state.current_version,
                "has_news": r.state.current_version > 0,
            }
        )
    return {"items": items, "note": GENERATED_NOTE}


@eventnews_api.get("/{item_id}")
def get_event_news(item_id: str) -> dict[str, Any]:
    """1 事象の詳細 — 生成本文 + 構成記事 (全件)。"""
    repo = _repo()
    record = repo.get_event_item(item_id)
    if record is None:
        raise HTTPException(status_code=404, detail="not found")
    if record.merged_into:
        raise HTTPException(status_code=404, detail=f"merged into {record.merged_into}")
    return {
        "id": record.state.item_id,
        "status": record.state.status,
        "change_kind": record.change_kind,
        "importance": record.state.importance,
        "independent_sources": record.independent_sources,
        "state_media_count": record.state_media_count,
        "unclassified_sources": record.unclassified_sources,
        "best_source_tier": record.best_source_tier,
        "first_reported_at": record.state.first_reported_at.isoformat(),
        "last_reported_at": record.state.last_reported_at.isoformat(),
        "news": _version_payload(repo, item_id),
        "members": _members_payload(repo, item_id),
        # 原記事から抽出済みのメタデータ (決定論の集約。生成本文とは別枠で出す)
        "metadata": _metadata_payload(repo, record.state.member_ids),
        "note": GENERATED_NOTE,
    }
