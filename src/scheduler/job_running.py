"""実行中ジョブの台帳 (プロセス内) — 二重起動の防止と「実行中」表示の元 (2026-09-26)。

同じジョブが 3 つの経路から起動されうる: スケジューラの定時発火 / 毎時チェーンの段 /
手動実行 (と自動復旧 watchdog)。従来は互いを知らず、
- 手動実行は APScheduler の次回時刻を書き換えるだけで、実行中なら同時実行上限で黙って捨てられた
- チェーンの段を手動で起動すると、同じ段をチェーンが並行して走らせた
3 経路とも ``running_slot`` を通すことで、同じ job_id は同時に 1 本しか走らない。

scheduler を持つのは full instance だけなので、この台帳もそこにしか無い。別プロセス
(readonly instance) からの表示用には、呼出側が DB (job_last_run) にも running を書く。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

_RUNNING: dict[str, datetime] = {}


class JobBusyError(RuntimeError):
    """同じジョブ (またはそれを含むチェーン) が実行中で、起動できない。"""


def is_running(job_id: str) -> bool:
    return job_id in _RUNNING


def running_since(job_id: str) -> datetime | None:
    return _RUNNING.get(job_id)


def running_jobs() -> dict[str, datetime]:
    """実行中ジョブの写し (呼出側が変更しても台帳に影響しない)。"""
    return dict(_RUNNING)


@asynccontextmanager
async def running_slot(job_id: str) -> AsyncIterator[bool]:
    """job_id の実行枠を取る。既に実行中なら False を yield し、枠は取らない。"""
    if job_id in _RUNNING:
        yield False
        return
    _RUNNING[job_id] = datetime.now(UTC)
    try:
        yield True
    finally:
        _RUNNING.pop(job_id, None)
