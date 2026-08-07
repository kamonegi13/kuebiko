"""プロダクト配信ルーティング管理 API (通知再設計)。

GET  /api/v1/product-routing   product → channel マッピング + 選択可能 channel + ラベル
POST /api/v1/product-routing   検証して DB (config_store) に版保存 + キャッシュ無効化

curated product (朝刊/夕刊・weekly recap・synthesis・spotlight) の配信先 channel を
情報フローから編集する。記事ルール (routing_rules) とは別 config (条件マッチでなく名前写像)。
write は READ_ONLY middleware (app.py) が 403 で block するため個別ガード不要。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger

product_routing_api = APIRouter(prefix="/api/v1/product-routing", tags=["product-routing"])

_log = get_logger(__name__)

# product id → UI 表示ラベル (curated product の人間可読名)。
PRODUCT_LABELS: dict[str, str] = {
    "morning_brief": "朝ブリーフィング (06:30)",
    "evening_brief": "夕ブリーフィング (19:30)",
    "weekly_recap": "週次深掘りダイジェスト",
    "status_synthesis": "状況総括 (daily/weekly/monthly)",
    "pir_spotlight": "PIR スポットライト",
}


class SaveProductRoutingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routing: dict[str, str]


@product_routing_api.get("")
async def get_product_routing() -> dict[str, Any]:
    """プロダクト配信マッピング (UI 編集用)。channel の push 状況も同梱。"""
    from src.tools.channel_registry import load_channels
    from src.tools.product_routing import (
        BUILTIN_PRODUCT_ROUTING,
        invalidate_product_routing_cache,
        load_product_routing,
    )

    invalidate_product_routing_cache()
    routing = load_product_routing()
    channels = load_channels()
    return {
        "routing": routing,
        "labels": PRODUCT_LABELS,
        "builtin": BUILTIN_PRODUCT_ROUTING,
        # 選択肢 + 各 channel が push (配信) か web-only (保存のみ) か
        "channels": [{"id": c.id, "label": c.label or c.id, "push": c.push} for c in channels],
    }


@product_routing_api.post("")
async def save_product_routing(req: SaveProductRoutingRequest) -> dict[str, Any]:
    """プロダクト配信マッピングを検証して保存 (版履歴は config_store に残る)。"""
    from src.storage.config_store import save_config
    from src.tools.channel_registry import known_channel_ids
    from src.tools.product_routing import (
        PRODUCT_ROUTING_CONFIG_KEY,
        invalidate_product_routing_cache,
        validate_product_routing,
    )

    errs = validate_product_routing(req.routing, known_channels=set(known_channel_ids()))
    if errs:
        raise HTTPException(status_code=400, detail="; ".join(errs))

    version = save_config(PRODUCT_ROUTING_CONFIG_KEY, req.routing, note="UI 保存 (プロダクト配信)")
    invalidate_product_routing_cache()
    _log.info("product_routing_saved", count=len(req.routing), version=version)
    return {"saved": True, "count": len(req.routing), "version": version}
