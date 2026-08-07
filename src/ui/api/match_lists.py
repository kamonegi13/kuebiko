"""語彙拡張② マッチリスト管理 API。

GET  /api/v1/match-lists   現行の user 定義リスト一覧
POST /api/v1/match-lists   検証して DB (config_store) に版保存 + キャッシュ無効化

write は READ_ONLY middleware (app.py) が 403 で block するため個別ガード不要。
版履歴は config-history (key=match_lists) で閲覧/revert できる。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger

match_lists_api = APIRouter(prefix="/api/v1/match-lists", tags=["match-lists"])

_log = get_logger(__name__)


class SaveMatchListsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lists: list[dict[str, Any]]


@match_lists_api.get("")
async def get_match_lists_route() -> dict[str, Any]:
    """user 定義マッチリスト一覧 (UI 編集用)。"""
    from src.cti.match_lists import get_match_lists

    lists = get_match_lists(force_reload=True)
    return {
        "lists": [
            {"name": ml.name, "description": ml.description, "terms": list(ml.terms)}
            for ml in lists
        ],
    }


@match_lists_api.post("")
async def save_match_lists_route(req: SaveMatchListsRequest) -> dict[str, Any]:
    """マッチリストを検証して保存 (版履歴は config_store に残る)。"""
    from src.cti.match_lists import save_match_lists, validate_match_lists

    errs = validate_match_lists(req.lists)
    if errs:
        raise HTTPException(status_code=400, detail="; ".join(errs))
    version = save_match_lists(req.lists, note="UI 保存 (マッチリスト)")
    _log.info("match_lists_saved", count=len(req.lists), version=version)
    return {"saved": True, "count": len(req.lists), "version": version}
