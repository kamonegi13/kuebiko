"""台帳更新の排他 (src/assessment/ledger_lock.py)。

定時 run と毎時の増分再評価が同じ台帳を並行評価すると、先に読み始めた側が後から書き、
古い証拠の判定が最新の revision になる (2026-09-21〜25 に 24 件)。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.assessment import ledger_lock
from src.storage import db_backend


class _FakeConn:
    """pg_try_advisory_lock の返り値を順に返す偽の接続。"""

    def __init__(self, results: list[bool]) -> None:
        self._results = list(results)
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql: str, params: tuple[Any, ...]) -> _FakeConn:
        self.executed.append(sql)
        self._last = self._results.pop(0) if "try_advisory" in sql else True
        return self

    def fetchone(self) -> tuple[bool]:
        return (self._last,)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def pg(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(db_backend, "is_pg_enabled", lambda: True)
    monkeypatch.setattr(ledger_lock, "POLL_SECONDS", 0.0)

    def install(conn: _FakeConn | Exception) -> None:
        def connect() -> _FakeConn:
            if isinstance(conn, Exception):
                raise conn
            return conn

        monkeypatch.setattr(ledger_lock, "_connect", connect)

    return install


async def test_sqlite_mode_does_not_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db_backend, "is_pg_enabled", lambda: False)

    async with ledger_lock.ledger_write_lock(holder="t") as acquired:
        assert acquired is True


async def test_acquires_and_releases_when_free(pg: Any) -> None:
    conn = _FakeConn([True])
    pg(conn)

    async with ledger_lock.ledger_write_lock(holder="t") as acquired:
        assert acquired is True

    assert any("advisory_unlock" in q for q in conn.executed)
    assert conn.closed


async def test_no_wait_gives_up_when_busy_without_unlocking(pg: Any) -> None:
    conn = _FakeConn([False])
    pg(conn)

    async with ledger_lock.ledger_write_lock(holder="t") as acquired:
        assert acquired is False

    # 持っていない lock を外そうとしない
    assert not any("advisory_unlock" in q for q in conn.executed)
    assert conn.closed


async def test_waits_until_holder_finishes(pg: Any) -> None:
    conn = _FakeConn([False, False, True])
    pg(conn)

    async with ledger_lock.ledger_write_lock(wait_seconds=60, holder="t") as acquired:
        assert acquired is True

    assert sum("try_advisory" in q for q in conn.executed) == 3


async def test_wait_times_out(pg: Any) -> None:
    conn = _FakeConn([False] * 1000)
    pg(conn)

    async with ledger_lock.ledger_write_lock(wait_seconds=0.01, holder="t") as acquired:
        assert acquired is False
    assert conn.closed


async def test_connect_failure_yields_false(pg: Any) -> None:
    pg(OSError("down"))

    async with ledger_lock.ledger_write_lock(holder="t") as acquired:
        assert acquired is False
