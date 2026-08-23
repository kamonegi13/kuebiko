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
from typing import Any

from fastapi import APIRouter, HTTPException

from src.cti.source_basis import classify_source_tier
from src.storage.run_history import RunHistoryRepository

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
    latest = versions[-1]
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


def _headline_and_preview(repo: RunHistoryRepository, record: Any) -> tuple[str, str]:
    """一覧に出す見出しと冒頭。生成があればそれを、無ければ原記事のものを使う。"""
    if record.state.current_version > 0:
        versions = repo.list_event_versions(record.state.item_id)
        if versions:
            latest = versions[-1]
            body = json.loads(latest.body_json) if latest.body_json else {}
            return latest.headline, str(body.get("bluf", ""))[:160]
    articles = repo.get_articles_by_ids(list(record.state.member_ids))
    for aid in record.state.member_ids:
        art = articles.get(aid)
        if art is not None:
            return art.title, (art.summary or "")[:160]
    return "(記事の取得に失敗)", ""


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
    wanted = {i.strip() for i in importance.split(",")} if importance else None
    records = repo.list_event_items(
        origin="live", statuses=statuses, limit=min(limit, _LIST_LIMIT_MAX)
    )
    items = []
    for r in records:
        if r.merged_into:
            continue  # 墓標は一覧に出さない (redirect 先が出る)
        if wanted and r.state.importance not in wanted:
            continue
        headline, preview = _headline_and_preview(repo, r)
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
        "note": GENERATED_NOTE,
    }
