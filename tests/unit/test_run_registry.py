"""src.ui.services.run_registry のテスト (Phase 1.5b)。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import cast

import pytest

from src.ui.services.run_registry import LogEvent, RunRegistry


class TestSubscribeBroadcast:
    @pytest.mark.asyncio
    async def test_single_subscriber_receives_events(self) -> None:
        reg = RunRegistry()
        received: list[LogEvent] = []

        async def consumer() -> None:
            async for event in reg.subscribe(run_id=1):
                received.append(event)
                if event.type == "done":
                    break

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0)  # subscribe を確立
        await reg.broadcast(1, LogEvent(type="line", seq=1, line="hello"))
        await reg.broadcast(1, LogEvent(type="line", seq=2, line="world"))
        await reg.complete(1, status="succeeded")
        await asyncio.wait_for(task, timeout=2.0)

        assert [e.type for e in received] == ["line", "line", "done"]
        assert [e.line for e in received[:2]] == ["hello", "world"]
        assert received[-1].status == "succeeded"

    @pytest.mark.asyncio
    async def test_multi_subscribers_each_receive_events(self) -> None:
        reg = RunRegistry()
        a: list[LogEvent] = []
        b: list[LogEvent] = []

        async def consume(out: list[LogEvent]) -> None:
            async for e in reg.subscribe(run_id=42):
                out.append(e)
                if e.type == "done":
                    break

        ta = asyncio.create_task(consume(a))
        tb = asyncio.create_task(consume(b))
        await asyncio.sleep(0)
        await reg.broadcast(42, LogEvent(type="line", seq=1, line="hi"))
        await reg.complete(42, status="succeeded")
        await asyncio.wait_for(asyncio.gather(ta, tb), timeout=2.0)

        assert len(a) == 2
        assert len(b) == 2
        assert a[0].line == "hi"
        assert b[0].line == "hi"

    @pytest.mark.asyncio
    async def test_unsubscribe_on_break(self) -> None:
        reg = RunRegistry()

        async def consume_one() -> None:
            sub = cast(AsyncGenerator[LogEvent, None], reg.subscribe(run_id=7))
            try:
                async for _ in sub:
                    break
            finally:
                await sub.aclose()

        task = asyncio.create_task(consume_one())
        await asyncio.sleep(0)
        assert reg.subscriber_count(7) == 1
        await reg.broadcast(7, LogEvent(type="line", seq=1, line="x"))
        await asyncio.wait_for(task, timeout=2.0)
        assert reg.subscriber_count(7) == 0

    @pytest.mark.asyncio
    async def test_complete_clears_registry(self) -> None:
        reg = RunRegistry()

        async def consume() -> None:
            async for e in reg.subscribe(run_id=99):
                if e.type == "done":
                    break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        await reg.complete(99, status="succeeded")
        await asyncio.wait_for(task, timeout=2.0)
        assert reg.subscriber_count(99) == 0
        assert reg.is_running(99) is False

    @pytest.mark.asyncio
    async def test_backpressure_emits_marker(self) -> None:
        """queue 満杯時に backpressure イベントが発火し done は確実に届く。"""
        reg = RunRegistry(queue_maxsize=2)
        events: list[LogEvent] = []

        # subscribe iterator を作って初期化のため 1 件目を消費
        sub: AsyncIterator[LogEvent] = reg.subscribe(run_id=1)

        async def init() -> LogEvent:
            return await sub.__anext__()

        # broadcast → 1 件目を消費 (Queue を登録するため)
        broadcast_task = asyncio.create_task(
            reg.broadcast(1, LogEvent(type="line", seq=1, line="initial")),
        )
        first = await asyncio.wait_for(init(), timeout=2.0)
        events.append(first)
        await broadcast_task

        # 連続して maxsize 超の broadcast を行う (この間 consumer は走らない)
        for i in range(2, 7):
            await reg.broadcast(1, LogEvent(type="line", seq=i, line=f"L{i}"))
        await reg.complete(1, status="succeeded")

        # 残りを消費
        try:
            async for event in sub:
                events.append(event)
                if event.type == "done":
                    break
        finally:
            await sub.aclose()  # type: ignore[attr-defined]

        assert events[0].line == "initial"
        assert events[-1].type == "done"
        assert any(e.type == "backpressure" for e in events)
