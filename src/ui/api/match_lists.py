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
def get_match_lists_route() -> dict[str, Any]:
    """user 定義マッチリスト一覧 (UI 編集用)。"""
    from src.cti.definition_usage import match_list_usage
    from src.cti.match_lists import get_match_lists
    from src.cti.routing_rules import load_routing_rules

    lists = get_match_lists(force_reload=True)
    names = [ml.name for ml in lists]
    # 参照関係 (S2): 「この語彙を誰が使っているか」を編集画面に出す。
    # 定義の変更を設定へ寄せる前提条件 (docs/settings_consolidation_plan.md §4) で、
    # これが無いと旧基準と同じ「理屈だけ」の状態になる。
    # ⚠ 参照ゼロを「削除してよい」と読ませない — 事実だけ返し判断は利用者に委ねる。
    try:
        usage = match_list_usage(names, list(load_routing_rules()))
    except Exception as e:  # noqa: BLE001 — 参照が引けなくても編集は壊さない
        _log.warning("match_list_usage_failed", error=str(e))
        usage = dict.fromkeys(names, [])
    return {
        "lists": [
            {
                "name": ml.name,
                "description": ml.description,
                "terms": list(ml.terms),
                "used_by_rules": usage.get(ml.name, []),
            }
            for ml in lists
        ],
    }


@match_lists_api.post("")
def save_match_lists_route(req: SaveMatchListsRequest) -> dict[str, Any]:
    """マッチリストを検証して保存 (版履歴は config_store に残る)。"""
    from src.cti.match_lists import save_match_lists, validate_match_lists

    errs = validate_match_lists(req.lists)
    if errs:
        raise HTTPException(status_code=400, detail="; ".join(errs))
    version = save_match_lists(req.lists, note="UI 保存 (マッチリスト)")
    _log.info("match_lists_saved", count=len(req.lists), version=version)
    return {"saved": True, "count": len(req.lists), "version": version}
