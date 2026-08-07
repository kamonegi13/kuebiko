"""情報フロー API (優先度の背骨: PIR → 重要度 → 配信ルール → チャンネル)。

GET /api/v1/flow?days=7 — フロー図 1 画面分のデータを 1 応答で返す:
    - PIR ごとの期間内マッチ実数 (importance / channel 内訳)
    - importance → channel エッジの実数 (posted articles の実流量)
    - routing rules (ladder) + engine 有効状態
    - チャンネル (registry) + 期間内投稿数

編集はここでは扱わない (PIR=pir API / rules=routing-rules API / channels=channels API)。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from src.logging_config import get_logger

flow_api = APIRouter(prefix="/api/v1/flow", tags=["flow"])

_log = get_logger(__name__)


@flow_api.get("")
async def get_flow(days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    """情報フロー snapshot (期間内 posted articles が母集合)。"""
    from src.cti.router import is_rules_engine_enabled
    from src.cti.routing_rules import load_routing_rules
    from src.pir.evaluator import compute_flow_snapshot
    from src.pir.integration import get_pir_config
    from src.tools.channel_registry import invalidate_channels_cache, load_channels

    pir_cfg = get_pir_config(force_reload=True)
    pirs = pir_cfg.priorities
    snapshot = compute_flow_snapshot(pirs, lookback_hours=days * 24)

    # UI は常に最新の registry を見る (キャッシュは pipeline 用)
    invalidate_channels_cache()
    channels = load_channels()
    posted_by_channel: dict[str, int] = {}
    for _imp, ch, count in snapshot.importance_channel:
        posted_by_channel[ch] = posted_by_channel.get(ch, 0) + count

    return {
        "period_days": days,
        "engine_enabled": is_rules_engine_enabled(),
        "total_articles": snapshot.total_articles,
        "importance_totals": snapshot.importance_totals,
        "importance_channel": [
            {"importance": imp, "channel": ch, "count": count}
            for imp, ch, count in snapshot.importance_channel
        ],
        "pirs": [
            {
                "id": pir.id,
                "title": pir.title,
                "enabled": pir.enabled,
                "target_importance": pir.target_importance,
                "matched_total": stat.matched_total,
                "by_importance": stat.by_importance,
                "by_channel": stat.by_channel,
            }
            for pir, stat in zip(pirs, snapshot.pir_stats, strict=True)
        ],
        "rules": load_routing_rules(force_reload=True),
        "channels": [
            {
                "id": ch.id,
                "label": ch.label,
                "enabled": ch.enabled,
                "routable": ch.routable,
                "posted_count": posted_by_channel.get(ch.id, 0),
            }
            for ch in channels
        ],
    }
