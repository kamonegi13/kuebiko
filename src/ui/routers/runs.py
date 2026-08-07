"""即時実行 ``/runs`` (Phase 1.5b: SSE + DB ライブストリーミング)。

エンドポイント:
- ``GET  /runs``                  : 即時実行ページ。直近 running を誘導
- ``POST /runs/start``            : subprocess 起動 → run_id を確定し /runs/{id} へリダイレクト
- ``GET  /runs/{run_id}``         : run 詳細ページ (live_log + status)
- ``GET  /runs/{run_id}/log``     : ログ全文 (text/plain or JSON)
- ``GET  /runs/{run_id}/stream``  : SSE ライブストリーム
- ``GET  /runs/{run_id}/status``  : ステータス JSON

設計方針:
- `Last-Event-ID` ヘッダ (HTML5 EventSource 標準) で再接続時の seq 続きを実現
- subprocess の寿命と HTTP リクエストの寿命を分離 (BackgroundTask + asyncio.create_task)
- 終了済み run へのアクセスは DB から全行流して即 done
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.responses import StreamingResponse

from src.logging_config import get_logger
from src.storage.run_history import LogLine, RunHistoryRepository
from src.ui.services.pipeline_runner import PipelineRunner
from src.ui.services.run_registry import LogEvent, RunRegistry

router = APIRouter()
_log = get_logger(__name__)

# SSE keep-alive のタイムアウト (この間隔で : ping を送る)
SSE_KEEPALIVE_SECONDS = 15.0


@router.get("/runs")
async def runs_legacy_redirect() -> Response:
    """Legacy URL → React SPA (/app/runs)。"""
    return Response(status_code=302, headers={"Location": "/app/runs"})


@router.get("/runs/{run_id:int}")
async def run_detail_legacy_redirect(run_id: int) -> Response:
    """Legacy URL → React SPA (/app/run/{id})。"""
    return Response(status_code=302, headers={"Location": f"/app/run/{run_id}"})


@router.get("/runs/{run_id}/log", response_class=PlainTextResponse)
async def run_log(
    request: Request,
    run_id: int,
    from_seq: int = 0,
    fmt: str = "text",
) -> Response:
    """ログ全文を返す。``fmt=json`` で JSON 配列にもできる。"""
    repo: RunHistoryRepository = request.app.state.repo
    run = repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run_id={run_id} not found")
    lines = repo.get_log_lines(run_id, from_seq=from_seq)
    if fmt == "json":
        return JSONResponse(
            [
                {
                    "seq": line.seq,
                    "ts": line.ts.isoformat(),
                    "stream": line.stream,
                    "line": line.line,
                }
                for line in lines
            ],
        )
    return PlainTextResponse("\n".join(line.line for line in lines))


@router.get("/runs/{run_id}/status")
async def run_status(request: Request, run_id: int) -> JSONResponse:
    repo: RunHistoryRepository = request.app.state.repo
    run = repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run_id={run_id} not found")
    return JSONResponse(
        {
            "run_id": run.id,
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "log_line_count": run.log_line_count,
            "log_truncated": run.log_truncated,
            "dry_run": run.dry_run,
        },
    )


@router.get("/runs/{run_id}/stream")
async def run_stream(request: Request, run_id: int) -> StreamingResponse:
    """SSE: 既存ログを一気に流したあと、in-process registry に subscribe。"""
    repo: RunHistoryRepository = request.app.state.repo
    runner: PipelineRunner = request.app.state.runner
    registry: RunRegistry = runner.registry

    run = repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run_id={run_id} not found")

    # Last-Event-ID ヘッダで再接続時の続きを判定
    last_event_id = request.headers.get("last-event-id", "0")
    try:
        from_seq = max(int(last_event_id), 0)
    except ValueError:
        from_seq = 0

    async def event_stream() -> AsyncIterator[bytes]:
        # 1. DB から既存分を流す
        existing = repo.get_log_lines(run_id, from_seq=from_seq)
        for line in existing:
            yield _format_sse_line(line)
            if await request.is_disconnected():
                return

        # 2. 終了済み run なら done を送って close
        latest = repo.get_run(run_id)
        if latest is None or latest.status != "running":
            yield _format_sse_done(latest.status if latest else "unknown")
            return

        # 3. running の場合、registry に subscribe して増分配信
        sub = registry.subscribe(run_id)
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(
                        sub.__anext__(),
                        timeout=SSE_KEEPALIVE_SECONDS,
                    )
                except TimeoutError:
                    yield b": ping\n\n"
                    continue
                except StopAsyncIteration:
                    break
                yield _format_sse_event(event)
                if event.type == "done":
                    break
        finally:
            await sub.aclose()  # type: ignore[attr-defined]

    headers = {
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=headers,
    )


# ---------- SSE format helpers ----------


def _sse_line_payload(line: str, seq: int | None) -> str:
    """SSE の data: フィールド用ペイロード (改行が含まれる場合は複数 data 行に分割)。"""
    payload = json.dumps({"seq": seq, "line": line}, ensure_ascii=False)
    return payload


def _format_sse_line(line: LogLine) -> bytes:
    payload = _sse_line_payload(line.line, line.seq)
    return f"event: line\nid: {line.seq}\ndata: {payload}\n\n".encode()


def _format_sse_event(event: LogEvent) -> bytes:
    if event.type == "line":
        payload = _sse_line_payload(event.line or "", event.seq)
        seq_part = f"id: {event.seq}\n" if event.seq is not None else ""
        return f"event: line\n{seq_part}data: {payload}\n\n".encode()
    if event.type == "done":
        return _format_sse_done(event.status or "unknown")
    if event.type == "status":
        payload = json.dumps({"status": event.status}, ensure_ascii=False)
        return f"event: status\ndata: {payload}\n\n".encode()
    if event.type == "backpressure":
        payload = json.dumps({"line": event.line or ""}, ensure_ascii=False)
        return f"event: backpressure\ndata: {payload}\n\n".encode()
    # ping その他
    return b": ping\n\n"


def _format_sse_done(status: str) -> bytes:
    payload = json.dumps({"status": status}, ensure_ascii=False)
    return f"event: done\ndata: {payload}\n\n".encode()
