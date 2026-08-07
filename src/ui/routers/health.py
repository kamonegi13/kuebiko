"""``/health`` legacy URL を React SPA に redirect。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()


@router.get("/health")
async def health_legacy_redirect() -> Response:
    # /app/health は廃止 (設定・死活の画面統合 P4) — 死活はダッシュボード widget と
    # 各対象画面 (チャンネル/ソース/モデル) に統合。
    return Response(status_code=302, headers={"Location": "/app/"})
