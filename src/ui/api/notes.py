"""Phase 5 (学習・記憶): 記事への memo / bookmark / tag / judgment API。

アナリストの判断・気づきを記事に紐づけて蓄積する。write 系 (PUT/DELETE) は
READ_ONLY instance では middleware が 403 で block するため、公開 readonly からは
編集不可 (閲覧専用)。将来の過去参照 (Phase 6) / 学習の基盤データになる。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.storage.records import EventNoteRecord
from src.storage.run_history import ArticleNoteRecord

notes_api = APIRouter(prefix="/api/v1", tags=["notes"])


class NoteBody(BaseModel):
    bookmarked: bool = False
    note: str = ""
    tags: list[str] = Field(default_factory=list)
    judgment: str = ""


def note_to_dict(n: ArticleNoteRecord) -> dict[str, Any]:
    return {
        "article_id": n.article_id,
        "bookmarked": n.bookmarked,
        "note": n.note,
        "tags": n.tags,
        "judgment": n.judgment,
        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
    }


@notes_api.get("/notes")
def list_notes(
    request: Request,
    bookmarked_only: bool = Query(default=False),
    tag: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """memo/bookmark を更新新しい順に列挙 (記事 title/url 解決込み)。read-only。"""
    repo = request.app.state.repo
    notes = repo.list_article_notes(bookmarked_only=bookmarked_only, tag=tag, limit=limit)
    arts = repo.get_articles_by_ids([n.article_id for n in notes])
    items: list[dict[str, Any]] = []
    for n in notes:
        a = arts.get(n.article_id)
        items.append(
            {
                **note_to_dict(n),
                "title": a.title if a else n.article_id,
                "url": a.url if a else "",
                "importance": a.importance if a else None,
                "category": a.category if a else None,
            }
        )
    return {"notes": items, "count": len(items)}


@notes_api.get("/notes/{article_id:path}")
def get_note(request: Request, article_id: str) -> dict[str, Any]:
    """1 article の note を取得 (無ければ exists=false)。read-only。"""
    repo = request.app.state.repo
    n = repo.get_article_note(article_id.strip())
    if n is None:
        return {"article_id": article_id, "exists": False}
    return {**note_to_dict(n), "exists": True}


@notes_api.put("/notes/{article_id:path}")
def put_note(request: Request, article_id: str, body: NoteBody) -> dict[str, Any]:
    """note を upsert。全フィールド空なら delete (no-op note を残さない)。write。"""
    repo = request.app.state.repo
    aid = article_id.strip()
    if not aid:
        raise HTTPException(status_code=400, detail="article_id は必須")
    existing = repo.get_article_note(aid)
    created = existing.created_at if existing else datetime.now(UTC)
    rec = ArticleNoteRecord(
        article_id=aid,
        bookmarked=body.bookmarked,
        note=body.note.strip(),
        tags=[t.strip() for t in body.tags if t.strip()],
        judgment=body.judgment.strip(),
        created_at=created,
    )
    if not (rec.bookmarked or rec.note or rec.tags or rec.judgment):
        repo.delete_article_note(aid)
        return {"article_id": aid, "exists": False}
    repo.upsert_article_note(rec)
    return {**note_to_dict(rec), "exists": True}


@notes_api.delete("/notes/{article_id:path}")
def delete_note(request: Request, article_id: str) -> dict[str, Any]:
    """note を削除。write。"""
    repo = request.app.state.repo
    repo.delete_article_note(article_id.strip())
    return {"article_id": article_id, "exists": False}


# ---- 事象単位の memo / bookmark (2026-08-24) ----------------------------------
#
# 事象ニュースの read API (`/api/v1/eventnews`) は **GET のみ / Tier0 匿名可** の
# 約束なので、write はこちら (notes) に置く。READ_ONLY instance では middleware が
# PUT を 403 で block する = 公開面からは編集不可。


def event_note_to_dict(n: EventNoteRecord) -> dict[str, Any]:
    return {
        "item_id": n.item_id,
        "bookmarked": n.bookmarked,
        "note": n.note,
        "tags": n.tags,
        "judgment": n.judgment,
        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
    }


@notes_api.get("/event-notes/{item_id}")
def get_event_note(request: Request, item_id: str) -> dict[str, Any]:
    """1 事象の memo/bookmark を返す (未作成なら空)。read-only。"""
    rec = request.app.state.repo.get_event_note(item_id.strip())
    if rec is None:
        return {"item_id": item_id, "bookmarked": False, "note": "", "tags": [], "judgment": ""}
    return event_note_to_dict(rec)


@notes_api.put("/event-notes/{item_id}")
def put_event_note(request: Request, item_id: str, body: NoteBody) -> dict[str, Any]:
    """1 事象の memo/bookmark を保存する (write = ローカル instance のみ)。"""
    iid = item_id.strip()
    if not iid:
        raise HTTPException(status_code=400, detail="item_id は必須")
    repo = request.app.state.repo
    if repo.get_event_item(iid) is None:
        raise HTTPException(status_code=404, detail=f"事象が見つかりません: {iid}")
    existing = repo.get_event_note(iid)
    repo.upsert_event_note(
        EventNoteRecord(
            item_id=iid,
            bookmarked=body.bookmarked,
            note=body.note,
            tags=body.tags,
            judgment=body.judgment,
            created_at=existing.created_at if existing else datetime.now(UTC),
        )
    )
    saved = repo.get_event_note(iid)
    return event_note_to_dict(saved) if saved else {"item_id": iid}
