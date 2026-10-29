"""scripts/readonly_conn.py: PG のときだけセッションを読み取り専用にする。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import readonly_conn  # noqa: E402


class _FakeConn:
    def __init__(self, fail: bool = False) -> None:
        self.statements: list[str] = []
        self.closed = False
        self._fail = fail

    def execute(self, sql: str) -> None:
        if self._fail:
            raise RuntimeError("boom")
        self.statements.append(sql)

    def close(self) -> None:
        self.closed = True


def test_pg_session_is_set_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.storage.db_backend.is_pg_enabled", lambda: True)
    conn = _FakeConn()
    got = readonly_conn.open_read_only(lambda _p: conn, "x")
    assert got is conn
    assert conn.statements == ["SET default_transaction_read_only = on"]


def test_sqlite_connection_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.storage.db_backend.is_pg_enabled", lambda: False)
    conn = _FakeConn()
    readonly_conn.open_read_only(lambda _p: conn, "x")
    assert conn.statements == []


def test_failure_to_set_closes_connection_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.storage.db_backend.is_pg_enabled", lambda: True)
    conn: Any = _FakeConn(fail=True)
    with pytest.raises(RuntimeError):
        readonly_conn.open_read_only(lambda _p: conn, "x")
    assert conn.closed
