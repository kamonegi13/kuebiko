"""新しい表のテストの型 (P0-10、設計 R10)。

新しい観測用の表を足すバッチは、表ごとのテストから ``assert_table_contract`` を 1 回呼ぶ。
検査するのは次の 4 つ (R10 の「両 schema・roundtrip・purge・公開面に出さない」):

1. SQLite の schema と PG の schema の両方に表があり、列の集合が一致する。
2. 全欄の roundtrip: 書いた行の全列 (``auto_columns`` を除く) が読み戻せて値が等しい。
   書き手が一部の列を書き忘れる・読み手が返し忘れる、を機械的に見つける。
3. purge が登録されている: repo に purge の method があり、定期保守
   (``src/ui/services/maintenance.py``) から呼ばれている。
4. API を持つ表は、そのパスが匿名で読めない (``READ_ONLY_GET_DENYLIST`` の対象)。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection, Mapping
from pathlib import Path

from src.storage.pg_schema import get_schema_sql
from src.storage.run_history import RunHistoryRepository
from src.ui.read_only_policy import is_read_only_blocked_get

_MAINTENANCE_SOURCE = Path("src/ui/services/maintenance.py")
_DEFAULT_AUTO_COLUMNS: frozenset[str] = frozenset({"id"})


def sqlite_columns(db_path: Path, table: str) -> set[str]:
    """実 DB を introspect した SQLite 側の列 (migration 込み)。"""
    conn = sqlite3.connect(db_path)
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


def pg_columns(table: str) -> set[str]:
    """PG の schema SQL の text parse による列 (CREATE TABLE + ADD COLUMN)。"""
    sql = get_schema_sql()
    cols: set[str] = set()
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);", sql, re.S)
    if match:
        for raw in match.group(1).splitlines():
            line = raw.strip().rstrip(",")
            first = re.split(r"[(\s]", line, maxsplit=1)[0]
            if line and not line.startswith("--") and first.upper() not in _CONSTRAINT_WORDS:
                cols.add(first)
    for m in re.finditer(rf"ALTER TABLE {table}\s+ADD COLUMN IF NOT EXISTS (\w+)", sql):
        cols.add(m.group(1))
    return cols


_CONSTRAINT_WORDS = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}


def assert_roundtrip(
    columns: Collection[str],
    written: Mapping[str, object],
    read_back: Mapping[str, object],
    *,
    auto_columns: Collection[str] = _DEFAULT_AUTO_COLUMNS,
) -> None:
    """表の全列 (``auto_columns`` を除く) が書かれ、同じ値で読み戻せることを検査する。"""
    expected = set(columns) - set(auto_columns)
    unwritten = expected - set(written)
    assert not unwritten, f"roundtrip の行が書いていない列: {sorted(unwritten)}"
    unread = expected - set(read_back)
    assert not unread, f"読み戻しに無い列: {sorted(unread)}"
    mismatched = [c for c in sorted(expected) if written[c] != read_back[c]]
    assert not mismatched, f"書いた値と読み戻した値が違う列: {mismatched}"


def assert_purge_registered(purge_method: str) -> None:
    """repo に purge の method があり、定期保守から呼ばれている。"""
    assert hasattr(RunHistoryRepository, purge_method), f"repo に {purge_method} が無い"
    source = _MAINTENANCE_SOURCE.read_text(encoding="utf-8")
    assert f".{purge_method}(" in source, f"{purge_method} が maintenance から呼ばれていない"


def assert_table_contract(
    table: str,
    *,
    tmp_path: Path,
    written: Mapping[str, object],
    read_back: Mapping[str, object],
    purge_method: str,
    api_path: str | None = None,
    auto_columns: Collection[str] = _DEFAULT_AUTO_COLUMNS,
) -> None:
    """新しい表の契約 4 点 (両 schema の一致・全欄 roundtrip・purge・公開面に出さない)。"""
    repo = RunHistoryRepository(db_path=tmp_path / "contract.db")
    sqlite_cols = sqlite_columns(repo.db_path, table)
    assert sqlite_cols, f"SQLite の schema に表 {table} が無い"
    pg_cols = pg_columns(table)
    assert pg_cols, f"PG の schema に表 {table} が無い"
    assert sqlite_cols == pg_cols, (
        f"{table}: 列が両 schema で違う (SQLite のみ {sorted(sqlite_cols - pg_cols)}、"
        f"PG のみ {sorted(pg_cols - sqlite_cols)})"
    )
    assert_roundtrip(sqlite_cols, written, read_back, auto_columns=auto_columns)
    assert_purge_registered(purge_method)
    if api_path is not None:
        assert is_read_only_blocked_get(api_path), f"{api_path} が匿名で読める (denylist 漏れ)"
