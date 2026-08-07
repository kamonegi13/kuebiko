"""``/history`` legacy URL を React SPA に redirect。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()


@router.get("/history")
async def history_legacy_redirect() -> Response:
    return Response(status_code=302, headers={"Location": "/app/history"})
