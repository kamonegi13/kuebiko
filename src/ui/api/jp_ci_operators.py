"""JP 重要インフラ 指定事業者名簿の管理 API。

GET  /api/v1/jp-ci-operators   名簿一覧 (DB SSoT → BUILTIN fallback 済の実効値)。
POST /api/v1/jp-ci-operators   全量保存 (検証 → config_store 版保存 → cache 無効化)。

channels API と同パターン。版履歴 / revert は /api/v1/config-history/jp_ci_operators。
READ_ONLY instance では middleware が POST を 403 で遮断する (構造的 write 不可)。
名簿は**事業者名→分野のみ** — 技術・露出情報の列は追加しない (設計 doc §8 の確定原則)。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.cti.nisc_sectors import NISC_SECTORS

jp_ci_operators_api = APIRouter(prefix="/api/v1/jp-ci-operators", tags=["jp-ci-operators"])


class OperatorEntry(BaseModel):
    """名簿 1 行 (UI 編集単位)。"""

    id: str = Field(min_length=1, max_length=80)
    canonical: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=16)
    sector: str


class SaveOperatorsRequest(BaseModel):
    operators: list[OperatorEntry] = Field(max_length=1000)


@jp_ci_operators_api.get("")
async def get_operators() -> dict[str, Any]:
    """実効名簿 (DB → BUILTIN fallback 済) と分野ラベル。"""
    from src.cti.ci_operator_roster import load_operators
    from src.storage.config_store import list_history

    ops = load_operators()
    try:
        history = list_history("jp_ci_operators", limit=1)
        version = history[0].version if history else None
    except Exception:  # noqa: BLE001 — 履歴取得失敗は表示のみの欠落
        version = None
    return {
        "operators": [op.as_dict() for op in ops],
        "sectors": dict(NISC_SECTORS),
        "count": len(ops),
        "version": version,
    }


@jp_ci_operators_api.post("")
async def save_operators(req: SaveOperatorsRequest) -> dict[str, Any]:
    """名簿の全量保存。検証エラーは 400 (保存しない)。"""
    from src.cti.ci_operator_roster import invalidate_operators_cache, validate_operators
    from src.storage.config_store import save_config

    rows = [e.model_dump() for e in req.operators]
    errors = validate_operators(rows)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    version = save_config("jp_ci_operators", rows, note="UI 保存 (重要インフラ事業者名簿)")
    invalidate_operators_cache()
    return {"ok": True, "version": version, "count": len(rows)}
