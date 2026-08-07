"""Unit tests for src.storage.db_backend.

translate_sql() の SQL dialect 翻訳ルールが期待通りか検証する。
PG 接続テストは integration tests に分離 (psycopg / postgres 必要)。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.storage.db_backend import (
    connect,
    is_pg_enabled,
    translate_sql,
)


class TestTranslateSql:
    """SQLite-dialect SQL → PostgreSQL-dialect への自動翻訳。"""

    # ----- パラメータ -----

    def test_question_mark_to_percent_s(self) -> None:
        result = translate_sql("SELECT * FROM runs WHERE id = ?")
        assert result == "SELECT * FROM runs WHERE id = %s"

    def test_multiple_question_marks(self) -> None:
        result = translate_sql("INSERT INTO t (a, b, c) VALUES (?, ?, ?)")
        assert result == "INSERT INTO t (a, b, c) VALUES (%s, %s, %s)"

    # ----- AUTOINCREMENT -----

    def test_autoincrement_to_bigserial(self) -> None:
        result = translate_sql("id INTEGER PRIMARY KEY AUTOINCREMENT")
        assert "BIGSERIAL PRIMARY KEY" in result
        assert "AUTOINCREMENT" not in result

    # ----- PRAGMA -----

    def test_pragma_becomes_noop(self) -> None:
        assert translate_sql("PRAGMA journal_mode=WAL") == "SELECT 1"
        assert translate_sql("PRAGMA foreign_keys=ON") == "SELECT 1"
        assert translate_sql("PRAGMA table_info(runs)") == "SELECT 1"

    # ----- datetime helpers -----

    def test_datetime_now_to_now(self) -> None:
        result = translate_sql("INSERT INTO t (ts) VALUES (datetime('now'))")
        assert "NOW()" in result
        assert "datetime('now')" not in result

    def test_datetime_now_offset_param(self) -> None:
        # datetime('now', ?) → (NOW() + (%s)::interval)
        result = translate_sql("WHERE ts >= datetime('now', ?)")
        assert "(NOW() + (%s)::interval)" in result

    def test_datetime_now_literal_offset(self) -> None:
        # datetime('now', '-7 day') → (NOW() + INTERVAL '-7 day')
        result = translate_sql("WHERE ts >= datetime('now', '-7 day')")
        assert "(NOW() + INTERVAL '-7 day')" in result

    def test_datetime_col_unwrap(self) -> None:
        # datetime(col) → col (PG TIMESTAMPTZ 直接比較可能)
        result = translate_sql("WHERE datetime(created_at) >= ?")
        assert "datetime(created_at)" not in result
        # created_at は残る
        assert "created_at" in result

    def test_datetime_param_cast(self) -> None:
        # datetime(%s) → (%s)::timestamptz
        result = translate_sql("WHERE created_at >= datetime(%s)")
        assert "(%s)::timestamptz" in result

    # ----- INSERT OR IGNORE -----

    def test_insert_or_ignore_becomes_on_conflict(self) -> None:
        sql = "INSERT OR IGNORE INTO article_entities (a, b) VALUES (?, ?)"
        result = translate_sql(sql)
        assert "ON CONFLICT DO NOTHING" in result
        assert "INSERT OR IGNORE" not in result

    # ----- strftime / substr -----

    def test_strftime_date_format(self) -> None:
        result = translate_sql("SELECT strftime('%Y-%m-%d', created_at) FROM t")
        assert "to_char(created_at, 'YYYY-MM-DD')" in result

    def test_substr_date_format(self) -> None:
        result = translate_sql("SELECT substr(created_at, 1, 10) FROM t")
        assert "to_char(created_at, 'YYYY-MM-DD')" in result

    # ----- 順序組合せ -----

    def test_complex_query_combination(self) -> None:
        sql = (
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) "
            "FROM articles WHERE created_at >= datetime('now', ?) "
            "GROUP BY day"
        )
        result = translate_sql(sql)
        assert "to_char(created_at, 'YYYY-MM-DD')" in result
        assert "(NOW() + (%s)::interval)" in result

    def test_idempotent_already_pg(self) -> None:
        # 既に PG dialect なら変更されない
        sql = "SELECT * FROM runs WHERE id = %s"
        result = translate_sql(sql)
        assert result == sql


class TestIsPgEnabled:
    def test_returns_false_when_database_url_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert is_pg_enabled() is False

    def test_returns_true_when_database_url_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/dummy")
        # psycopg 不在ならどのみち False。 ここでは psycopg がいる前提
        from src.storage.db_backend import _PSYCOPG_AVAILABLE

        if _PSYCOPG_AVAILABLE:
            assert is_pg_enabled() is True


class TestConnectSqliteFallback:
    """DATABASE_URL 未設定なら通常の sqlite3 connection を返す。"""

    def test_returns_sqlite_connection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        db_path = tmp_path / "test.db"
        conn = connect(db_path)
        assert isinstance(conn, sqlite3.Connection)
        # 簡単な書き込み + 読み出し
        conn.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)")
        conn.execute("INSERT INTO t (b) VALUES (?)", ("hello",))
        row = conn.execute("SELECT b FROM t WHERE a = 1").fetchone()
        assert row[0] == "hello"
        conn.close()
