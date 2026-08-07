"""Database backend selector (Phase Y-3)。

``DATABASE_URL`` env が設定されていれば PostgreSQL (psycopg3) を使用、
未設定なら従来の SQLite (sqlite3) を使用する。

互換 connection wrapper を介して、既存の ``sqlite3.connect(path)`` 呼出
パターンをそのまま PG にも適用できるようにする。

主な互換層:
- パラメータスタイル: ``?`` → ``%s`` 自動翻訳
- ``cursor.execute(...).fetchall()`` で dict-like access (column name)
- ``conn.commit()`` / ``conn.close()`` / context manager
- ``conn.row_factory = sqlite3.Row`` は no-op (PG 側で dict_row 既定)
- SQLite-specific PRAGMA は no-op で受け流す
- ``INTEGER PRIMARY KEY AUTOINCREMENT`` を ``BIGSERIAL PRIMARY KEY`` に翻訳
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

# psycopg は optional import (SQLite-only 運用で依存させない)
try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    _PSYCOPG_AVAILABLE = True
except ImportError:
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    ConnectionPool = None  # type: ignore[misc,assignment]
    _PSYCOPG_AVAILABLE = False


def is_pg_enabled() -> bool:
    """``DATABASE_URL`` env から PG モードを判定。"""
    return bool(os.environ.get("DATABASE_URL")) and _PSYCOPG_AVAILABLE


def get_database_url() -> str:
    """``DATABASE_URL`` env を取得。 PG モード前提。"""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (use SQLite path instead)")
    return url


# ===== Connection pool (PG mode only) =====

_pool_lock = threading.Lock()
_pool: Any | None = None  # ConnectionPool (psycopg-pool)


def _get_pool() -> Any:
    """connection pool (lazy init)。"""
    global _pool  # noqa: PLW0603
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                if not _PSYCOPG_AVAILABLE:
                    raise RuntimeError("psycopg is not installed")
                _pool = ConnectionPool(
                    conninfo=get_database_url(),
                    min_size=1,
                    max_size=10,
                    kwargs={"row_factory": dict_row},
                    open=False,
                )
                _pool.open(wait=True, timeout=15)
    return _pool


def close_pool() -> None:
    """pool を明示的に閉じる (test / shutdown 用)。"""
    global _pool  # noqa: PLW0603
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


# ===== SQL dialect 翻訳 =====

_PARAM_PLACEHOLDER_RE = re.compile(r"(?<!\\)\?")  # backslash でエスケープされていない ?
_AUTOINCREMENT_RE = re.compile(
    r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
    re.IGNORECASE,
)
# datetime('now', %s) → (NOW() + (%s)::interval)
_DATETIME_NOW_OFFSET_RE = re.compile(
    r"datetime\(\s*'now'\s*,\s*%s\s*\)",
    re.IGNORECASE,
)
# datetime('now', '-7 day') → (NOW() + ('-7 day')::interval)
_DATETIME_NOW_LITERAL_OFFSET_RE = re.compile(
    r"datetime\(\s*'now'\s*,\s*'([^']+)'\s*\)",
    re.IGNORECASE,
)
# datetime('now') → NOW()
_DATETIME_NOW_RE = re.compile(r"datetime\(\s*'now'\s*\)", re.IGNORECASE)
# datetime(col_name) → col_name (PG は TIMESTAMPTZ 直接比較可能)
_DATETIME_COL_RE = re.compile(r"datetime\(\s*([a-zA-Z_][\w.]*)\s*\)", re.IGNORECASE)
# datetime(%s) → (%s)::timestamptz (parameter style)
_DATETIME_PARAM_RE = re.compile(r"datetime\(\s*%s\s*\)", re.IGNORECASE)
# datetime('literal') → ('literal')::timestamptz
_DATETIME_LITERAL_RE = re.compile(r"datetime\(\s*('[^']*')\s*\)", re.IGNORECASE)
# INSERT OR IGNORE INTO → INSERT INTO ... (DO NOTHING は末尾に)
_INSERT_OR_IGNORE_RE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
# INSERT OR REPLACE INTO → INSERT INTO ... (REPLACE は別途 ON CONFLICT 必要)
_INSERT_OR_REPLACE_RE = re.compile(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", re.IGNORECASE)
# strftime('%Y-%m-%d', col) → to_char(col, 'YYYY-MM-DD')
_STRFTIME_DATE_RE = re.compile(
    r"strftime\(\s*['\"]%Y-%m-%d['\"]\s*,\s*([a-zA-Z_][\w.]*)\s*\)",
    re.IGNORECASE,
)
# substr(date_col, 1, 10) → to_char(date_col, 'YYYY-MM-DD')
# SQLite で TEXT date を YYYY-MM-DD 切り出しに使う pattern
_SUBSTR_DATE_RE = re.compile(
    r"substr\(\s*([a-zA-Z_][\w.]*)\s*,\s*1\s*,\s*10\s*\)",
    re.IGNORECASE,
)
# GROUP_CONCAT(col) → STRING_AGG(col, ',') — PG に GROUP_CONCAT は無い。
# 監査 2026-07-05 P6: taxonomy 検出器 2 本が Phase Y 以降 PG で沈黙死していた翻訳漏れ。
_GROUP_CONCAT_RE = re.compile(
    r"\bGROUP_CONCAT\(\s*([a-zA-Z_][\w.]*)\s*\)",
    re.IGNORECASE,
)
# INSTR(haystack, needle) → STRPOS(haystack, needle) — 引数順・意味 (1-based 位置 / 不在は 0)
# が一致。subject-gate の主題メンバーシップ照合を LIKE でなく position ベースで方言安全に
# 書くため (2026-07-27、docs/body_extraction_and_entity_integrity_redesign.md §5)。
_INSTR_RE = re.compile(r"\bINSTR\s*\(", re.IGNORECASE)


def translate_sql(sql: str) -> str:
    """sqlite SQL → PostgreSQL SQL を簡易翻訳する。"""
    # SQLite 固有 PRAGMA は PG では NOOP に置換
    if sql.strip().upper().startswith("PRAGMA"):
        return "SELECT 1"
    # AUTOINCREMENT → BIGSERIAL
    sql = _AUTOINCREMENT_RE.sub("BIGSERIAL PRIMARY KEY", sql)
    # ? → %s
    sql = _PARAM_PLACEHOLDER_RE.sub("%s", sql)
    # datetime helpers (順番重要: offset 版 → 引数なし版 → col 版)
    sql = _DATETIME_NOW_OFFSET_RE.sub("(NOW() + (%s)::interval)", sql)
    sql = _DATETIME_NOW_LITERAL_OFFSET_RE.sub(r"(NOW() + INTERVAL '\1')", sql)
    sql = _DATETIME_NOW_RE.sub("NOW()", sql)
    # parameter/literal は先に処理 (datetime(col) regex は %s や 'literal' に
    # match しないので順序問題なし、念のため param→literal→col の順)
    sql = _DATETIME_PARAM_RE.sub("(%s)::timestamptz", sql)
    sql = _DATETIME_LITERAL_RE.sub(r"(\1)::timestamptz", sql)
    sql = _DATETIME_COL_RE.sub(r"\1", sql)
    # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
    if _INSERT_OR_IGNORE_RE.search(sql):
        sql = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", sql)
        # 末尾に DO NOTHING (RETURNING 等が末尾にある場合は前に挿入)
        if " ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # strftime('%Y-%m-%d', col) → to_char(col, 'YYYY-MM-DD')
    sql = _STRFTIME_DATE_RE.sub(r"to_char(\1, 'YYYY-MM-DD')", sql)
    # substr(date_col, 1, 10) → to_char(date_col, 'YYYY-MM-DD')
    sql = _SUBSTR_DATE_RE.sub(r"to_char(\1, 'YYYY-MM-DD')", sql)
    # GROUP_CONCAT(col) → STRING_AGG(col, ',')
    sql = _GROUP_CONCAT_RE.sub(r"STRING_AGG(\1, ',')", sql)
    # INSTR(...) → STRPOS(...) — 関数名のみ置換 (引数順・意味が一致)
    sql = _INSTR_RE.sub("STRPOS(", sql)
    return sql


# ===== PG 互換 Row class =====


class _PgRow:
    """sqlite3.Row 互換: index / column name どちらでも access 可能。"""

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            # index access: dict の挿入順を信頼
            return list(self._data.values())[key]
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def keys(self) -> list[str]:
        return list(self._data.keys())

    def __iter__(self) -> Iterator[Any]:
        return iter(self._data.values())

    def __repr__(self) -> str:
        return f"_PgRow({self._data!r})"


# ===== PG Cursor wrapper (sqlite3.Cursor 互換) =====


class _PgCursor:
    """psycopg cursor を sqlite3.Cursor 互換 API で包む。"""

    def __init__(self, pg_cursor: Any) -> None:
        self._cur = pg_cursor

    def execute(self, sql: str, params: Any = None) -> _PgCursor:
        translated = translate_sql(sql)
        if params is None:
            self._cur.execute(translated)
        elif isinstance(params, dict):
            self._cur.execute(translated, params)
        else:
            # tuple / list
            self._cur.execute(translated, params)
        return self

    def executemany(self, sql: str, params_seq: Any) -> _PgCursor:
        translated = translate_sql(sql)
        self._cur.executemany(translated, params_seq)
        return self

    def fetchone(self) -> _PgRow | None:
        row = self._cur.fetchone()
        if row is None:
            return None
        return _PgRow(row) if isinstance(row, dict) else row

    def fetchall(self) -> list[_PgRow]:
        rows = self._cur.fetchall()
        return [_PgRow(r) if isinstance(r, dict) else r for r in rows]

    def fetchmany(self, size: int) -> list[_PgRow]:
        """sqlite3.Cursor.fetchmany 互換 (batch 反復用、iter_articles_with_body 等)。"""
        rows = self._cur.fetchmany(size)
        return [_PgRow(r) if isinstance(r, dict) else r for r in rows]

    def __iter__(self) -> Iterator[_PgRow]:
        for row in self._cur:
            yield _PgRow(row) if isinstance(row, dict) else row

    @property
    def lastrowid(self) -> int | None:
        """PG では LASTVAL() で直前 INSERT の sequence 値を取得。

        Note: SERIAL/IDENTITY 列を持たない table への INSERT 後では
        ``no value in current session`` で例外、その場合は None。
        """
        try:
            self._cur.execute("SELECT LASTVAL() AS lv")
            row = self._cur.fetchone()
            if row is None:
                return None
            # dict_row なので lv key
            return int(row["lv"]) if "lv" in row else int(list(row.values())[0])
        except Exception:  # noqa: BLE001 (LASTVAL 未設定など)
            return None

    @property
    def rowcount(self) -> int:
        return int(self._cur.rowcount)

    def close(self) -> None:
        self._cur.close()


# ===== PG Connection wrapper (sqlite3.Connection 互換) =====


class _PgConnection:
    """psycopg connection を sqlite3.Connection 互換 API で包む。"""

    def __init__(self, pg_conn: Any) -> None:
        self._conn = pg_conn
        # sqlite3.Connection が持つ属性 (no-op で受け流す)
        self.row_factory: Any = None

    def cursor(self) -> _PgCursor:
        return _PgCursor(self._conn.cursor())

    def execute(self, sql: str, params: Any = None) -> _PgCursor:
        """sqlite3.Connection.execute() 互換: 内部で cursor を作って execute。"""
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, params_seq: Any) -> _PgCursor:
        cur = self.cursor()
        cur.executemany(sql, params_seq)
        return cur

    def executescript(self, sql: str) -> None:
        """sqlite3.executescript 互換 (PG では cursor.execute で OK、 ; 区切り)。"""
        # PG では 1 つの execute で複数文 OK
        cur = self._conn.cursor()
        cur.execute(translate_sql(sql))
        cur.close()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        # pool に戻す
        pool = _get_pool()
        pool.putconn(self._conn)

    def __enter__(self) -> _PgConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()


# ===== Public API: connect() =====


def connect(
    db_path: Path | str | None = None,
    *,
    timeout: float = 30.0,
    isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = None,
    **kwargs: Any,
) -> Any:
    """sqlite3.connect() 互換の connection factory。

    DATABASE_URL env が set されていれば PG connection を返し、
    未設定なら通常の sqlite3.connect() を呼ぶ。

    Args:
        db_path: SQLite モード時の DB ファイル path。
            PG モードでは使われない (引数互換性のため受け取る)。
        timeout: SQLite モード時のロック待ち秒数。
        isolation_level: SQLite isolation level (PG では無視)。
        **kwargs: その他の sqlite3.connect の引数 (PG では無視)。
    """
    _ = isolation_level, kwargs  # 互換性のため受け取り

    if is_pg_enabled():
        pool = _get_pool()
        pg_conn = pool.getconn()
        # SQLite isolation_level=None (autocommit) と等価にする。
        # 各 execute() が独立 transaction で commit、 long-held lock を回避。
        try:  # noqa: SIM105 (best-effort、古い conn 状態でも継続)
            pg_conn.autocommit = True
        except Exception:  # noqa: BLE001 (古い conn 状態)
            pass
        wrapper = _PgConnection(pg_conn)
        return wrapper

    # SQLite legacy path
    if db_path is None:
        db_path = "data/run_history.db"
    return sqlite3.connect(
        db_path,
        timeout=timeout,
        isolation_level=isolation_level,
    )


@contextmanager
def transaction() -> Iterator[Any]:
    """新規 connection を開き、commit/rollback を保証する context manager。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_pg_schema_ensured = False


def ensure_pg_schema() -> None:
    """PG モードで schema を初期化する。process-level once-guard 付き (M-5)。"""
    global _pg_schema_ensured
    if _pg_schema_ensured or not is_pg_enabled():
        return
    from src.storage.pg_schema import get_schema_sql

    sql = get_schema_sql()
    with transaction() as conn:
        conn.executescript(sql)
    _pg_schema_ensured = True


__all__ = [
    "close_pool",
    "connect",
    "ensure_pg_schema",
    "get_database_url",
    "is_pg_enabled",
    "transaction",
    "translate_sql",
]
