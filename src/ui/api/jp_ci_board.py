"""日本重要インフラ 脅威 posture board API。

GET /api/v1/jp-ci-board  全 NISC 分野の posture (4 系統合成)。
read 専用集約。geo API と同じ in-process TTL cache + Query allowlist パターン。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Query

jp_ci_board_api = APIRouter(prefix="/api/v1/jp-ci-board", tags=["jp-ci-board"])

_TTL_SECONDS = 60.0
_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}


@jp_ci_board_api.get("")
def get_jp_ci_board(days: int = Query(default=30, ge=0, le=365)) -> dict[str, Any]:
    """全 NISC 分野の脅威 posture。``days=0`` で全期間。"""
    from src.ui.services.jp_ci_board import build_jp_ci_board
    from src.ui.services.standing_posture import build_standing_posture

    key = (days,)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and now - cached[0] < _TTL_SECONDS:
        return cached[1]
    data = build_jp_ci_board(window_days=days or None)
    # 段C: 常設情報要求 posture カード (第 3 の独立レンズ)。board 集計と独立に
    # situations/revisions から導出 (days 窓の影響を受けない)。未開設なら空 list。
    data["posture"] = build_standing_posture()
    # stale なエントリを掃除してから書く (無制限増加を防ぐ。key は days 有界だが明示的に purge)
    for k in [k for k, (ts, _) in _cache.items() if now - ts >= _TTL_SECONDS]:
        del _cache[k]
    _cache[key] = (now, data)
    return data
