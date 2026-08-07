"""run の in-process pub/sub レジストリ (Phase 1.5b: SSE 配信用)。

設計:
- 1 プロセス内で動く前提 (uvicorn 単一ワーカー)
- Phase 5 で複数ワーカー化する際は ``LogBroker`` Protocol に切り替えて
  Redis pub/sub 実装に差し替え可能
- 1 つの run に対して複数の subscriber (複数タブから同 run を見ている等) を
  サポート

挙動:
- ``broadcast(run_id, event)``: 該当 run を購読中の全 Queue に put
- ``subscribe(run_id)`` -> AsyncIterator[Event]: 切断時に必ず unsubscribe
- ``complete(run_id, status)``: ``done`` イベントを broadcast し registry から消す

backpressure:
- 各 Queue に maxsize を設けて、フル時は最古 1 件 drop + 警告イベントを broadcast
- 上限に達するのは subscriber 側がブロックされてイベントを取り出せていない場合のみ
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger

_log = get_logger(__name__)

# 各 subscriber Queue の上限。frontend が読み取りを止めても安全側に倒す。
DEFAULT_QUEUE_MAXSIZE = 1000

LogEventType = Literal["line", "status", "done", "ping", "backpressure"]


class LogEvent(BaseModel):
    """SSE で配信する単一イベント。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    type: LogEventType
    seq: int | None = None
    line: str | None = None
    status: str | None = None


class RunRegistry:
    """``dict[run_id, set[asyncio.Queue]]`` を中心にした in-process レジストリ。"""

    def __init__(self, queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> None:
        self._subs: dict[int, set[asyncio.Queue[LogEvent]]] = {}
        self._lock = asyncio.Lock()
        self._queue_maxsize = queue_maxsize

    async def broadcast(self, run_id: int, event: LogEvent) -> None:
        """run の全 subscriber に event を配信する。

        フル時は queue を drain して backpressure マーカー → 新 event の順で push する。
        ``done`` イベントが drop されないことを保証する。
        """
        async with self._lock:
            queues = list(self._subs.get(run_id, set()))
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._drain(q)
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(
                        LogEvent(
                            type="backpressure",
                            line="[backpressure: events dropped]",
                        ),
                    )
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(event)

    @staticmethod
    def _drain(q: asyncio.Queue[LogEvent]) -> None:
        """queue を可能な限り空にする (broadcast の retry 用)。"""
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def complete(self, run_id: int, status: str) -> None:
        """done イベントを送り、subscriber が抜けるまで待たずに registry を空にする。

        新規購読が来たら DB から復元するため、ここで registry に残す必要はない。
        """
        await self.broadcast(run_id, LogEvent(type="done", status=status))
        async with self._lock:
            self._subs.pop(run_id, None)

    async def subscribe(self, run_id: int) -> AsyncIterator[LogEvent]:
        """run の event を yield する非同期イテレータ。

        呼び出し側は ``async for event in registry.subscribe(run_id):`` で利用する。
        finally 節で必ず unsubscribe される。
        """
        queue: asyncio.Queue[LogEvent] = asyncio.Queue(maxsize=self._queue_maxsize)
        async with self._lock:
            self._subs.setdefault(run_id, set()).add(queue)
        try:
            while True:
                event = await queue.get()
                yield event
                if event.type == "done":
                    break
        finally:
            async with self._lock:
                subs = self._subs.get(run_id)
                if subs is not None:
                    subs.discard(queue)
                    if not subs:
                        self._subs.pop(run_id, None)

    def subscriber_count(self, run_id: int) -> int:
        return len(self._subs.get(run_id, set()))

    def is_running(self, run_id: int) -> bool:
        """registry にエントリがあれば broadcast 中 (= subprocess が実行中の可能性)。"""
        return run_id in self._subs
