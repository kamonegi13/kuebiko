"""SQLite _SCHEMA と pg_schema.py の parity test (監査 2026-07-05 P6)。

「新テーブル/新列は SQLite _SCHEMA と pg_schema.py の両方に追加必須」という既知
事故パターン (実 crash 2 回 + 今回監査で taxonomy_review_proposals の列 drift を検出)
に対する再発防御。SQLite 側は実 DB を組み立てて introspect (migration 込みの真の姿)、
PG 側は schema SQL の text parse で突合する。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from src.storage.pg_schema import get_schema_sql
from src.storage.run_history import RunHistoryRepository

# 意図的な差分の allowlist (理由必須)
# - decided_at: pg_schema 初版の残骸。taxonomy コードは reviewed_at/reviewed_by を使う。
#   live PG に列が存在するため DROP はせず、SQLite に足す価値もない (未使用)。
_ALLOWED_PG_EXTRA: dict[str, set[str]] = {
    "taxonomy_review_proposals": {"decided_at"},
}
_ALLOWED_SQLITE_EXTRA: dict[str, set[str]] = {}

_CONSTRAINT_KEYWORDS = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}


def _parse_pg_tables(sql: str) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", sql, re.S):
        name, body = m.group(1), m.group(2)
        cols: set[str] = set()
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            first = re.split(r"[(\s]", line, maxsplit=1)[0]
            if not first or first.upper() in _CONSTRAINT_KEYWORDS:
                continue
            cols.add(first)
        tables[name] = cols
    for m in re.finditer(r"ALTER TABLE (\w+)\s+ADD COLUMN IF NOT EXISTS (\w+)", sql):
        tables.setdefault(m.group(1), set()).add(m.group(2))
    return tables


def _sqlite_tables(tmp_path: Path) -> dict[str, set[str]]:
    # 実 repo を組み立てて introspect (executescript + _apply_migrations 込みの真の姿)
    repo = RunHistoryRepository(db_path=tmp_path / "parity.db")
    conn = sqlite3.connect(repo.db_path)
    try:
        names = [
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        return {
            n: {str(r[1]) for r in conn.execute(f"PRAGMA table_info({n})").fetchall()}
            for n in names
        }
    finally:
        conn.close()


def test_sqlite_and_pg_schema_have_same_tables_and_columns(tmp_path: Path) -> None:
    sqlite_tables = _sqlite_tables(tmp_path)
    pg_tables = _parse_pg_tables(get_schema_sql())

    problems: list[str] = []
    for table, sqlite_cols in sorted(sqlite_tables.items()):
        pg_cols = pg_tables.get(table)
        if pg_cols is None:
            problems.append(f"table {table}: PG schema に存在しない")
            continue
        sqlite_only = sqlite_cols - pg_cols - _ALLOWED_SQLITE_EXTRA.get(table, set())
        pg_only = pg_cols - sqlite_cols - _ALLOWED_PG_EXTRA.get(table, set())
        if sqlite_only:
            problems.append(f"table {table}: PG に無い列 {sorted(sqlite_only)}")
        if pg_only:
            problems.append(f"table {table}: SQLite に無い列 {sorted(pg_only)}")
    for table in sorted(set(pg_tables) - set(sqlite_tables)):
        problems.append(f"table {table}: SQLite schema に存在しない")

    assert not problems, "schema drift 検出:\n" + "\n".join(problems)
