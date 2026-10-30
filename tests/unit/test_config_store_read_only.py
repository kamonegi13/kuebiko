"""config_store の DDL が読み取り専用の接続で読みを止めない (TIER-LOAD-ALARM)。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from src.storage import config_store
from src.storage.config_store import get_config, is_read_only_error, save_config


class ReadOnlySqlTransaction(Exception):  # noqa: N818 — psycopg の型名を模す
    sqlstate = "25006"


def test_classifies_pg_and_sqlite_read_only() -> None:
    assert is_read_only_error(ReadOnlySqlTransaction("cannot execute CREATE TABLE"))
    assert is_read_only_error(sqlite3.OperationalError("attempt to write a readonly database"))


def test_other_errors_are_not_read_only() -> None:
    assert not is_read_only_error(sqlite3.OperationalError("unable to open database file"))
    assert not is_read_only_error(RuntimeError("permission denied"))


def test_normal_connection_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    save_config("k", {"a": 1}, db_path=db)
    assert get_config("k", db_path=db) == {"a": 1}


class _RoConn:
    """DDL だけ読み取り専用エラーで拒否する接続 (PG の読み取り専用 role の模擬)。"""

    def __init__(self, inner: sqlite3.Connection, err: Exception) -> None:
        self._inner = inner
        self._err = err
        self.rolled_back = False
        self.row_factory: Any = None

    def executescript(self, _sql: str) -> None:
        raise self._err

    def rollback(self) -> None:
        self.rolled_back = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_read_survives_read_only_ddl_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "c.db"
    save_config("k", {"a": 1}, db_path=db)
    conns: list[_RoConn] = []

    def fake_connect(_p: Any) -> _RoConn:
        c = _RoConn(sqlite3.connect(db), ReadOnlySqlTransaction("read-only"))
        c._inner.row_factory = sqlite3.Row
        conns.append(c)
        return c

    monkeypatch.setattr(config_store, "_backend_connect", fake_connect)

    assert get_config("k", db_path=db) == {"a": 1}
    assert conns[0].rolled_back


def test_other_ddl_error_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "c.db"
    monkeypatch.setattr(
        config_store,
        "_backend_connect",
        lambda _p: _RoConn(sqlite3.connect(db), sqlite3.OperationalError("disk I/O error")),
    )
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        get_config("k", db_path=db)


def test_write_on_read_only_connection_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "c.db"
    save_config("k", {"a": 1}, db_path=db)

    def ro_connect(_p: Any) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    monkeypatch.setattr(config_store, "_backend_connect", ro_connect)

    assert get_config("k", db_path=db) == {"a": 1}
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        save_config("k", {"a": 2}, db_path=db)
