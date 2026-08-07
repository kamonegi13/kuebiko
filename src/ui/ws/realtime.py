"""WebSocket real-time event push hub。

Pipeline / cron / 各種 publisher が ``broadcast()`` を呼び出すと、接続中の全 client に
JSON event を push する。React 側 (TanStack Query 連携) で receive → cache 無効化 →
即時 UI 反映を実現。

Event types:
    - article_posted     : 新規 article post 成功
    - synthesis_ready    : daily/weekly synthesis 生成完了
    - actor_new          : 新規 actor 初出現
    - spike_alert        : 既知 actor の spike 検出
    - kev_added          : KEV catalog 追加 (CVE-XXXX)
    - pipeline_running   : pipeline 開始 (progress 用)
    - pipeline_complete  : pipeline 完了

簡易実装: in-memory client set + asyncio.Queue。multi-process / horizontal scale
の場合は Redis pub/sub に差し替え可能 (今は単一プロセスなので OK)。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.logging_config import get_logger

_log = get_logger(__name__)

ws_router = APIRouter()

# In-memory client registry (single-process なので OK)
_clients: set[WebSocket] = set()
_clients_lock = asyncio.Lock()


async def _register(ws: WebSocket) -> None:
    async with _clients_lock:
        _clients.add(ws)
    _log.info("ws_client_registered", count=len(_clients))


async def _unregister(ws: WebSocket) -> None:
    async with _clients_lock:
        _clients.discard(ws)
    _log.info("ws_client_unregistered", count=len(_clients))


async def broadcast(event_type: str, payload: dict[str, Any] | None = None) -> None:
    """全 client に event を push。client 切断時は silently drop。"""
    msg = json.dumps({"type": event_type, "payload": payload or {}})
    async with _clients_lock:
        targets = list(_clients)
    if not targets:
        return
    dead: list[WebSocket] = []
    for ws in targets:
        try:
            await ws.send_text(msg)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    if dead:
        async with _clients_lock:
            for ws in dead:
                _clients.discard(ws)
    _log.info(
        "ws_broadcast",
        event_type=event_type,
        targets=len(targets),
        dropped=len(dead),
    )


@ws_router.websocket("/ws/v1/events")
async def events_ws(ws: WebSocket) -> None:
    """Client が subscribe する endpoint。受信側は send なし (push-only)。"""
    await ws.accept()
    await _register(ws)
    try:
        # Hello 送信 (server alive 確認用)
        await ws.send_text(json.dumps({"type": "hello", "payload": {"connected": True}}))
        # Client 側からの ping を受け取り続ける (close 検知)
        while True:
            try:
                await ws.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        await _unregister(ws)
