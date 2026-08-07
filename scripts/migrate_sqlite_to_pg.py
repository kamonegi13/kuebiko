"""SQLite → PostgreSQL migration script (Phase Y-3 cutover support)。

現行 data/run_history.db (SQLite) から PostgreSQL に既存データを copy する。
全テーブル対応。冪等 (PG 側に既に存在する row は skip)。

Usage:
    DATABASE_URL=postgresql://... uv run python scripts/migrate_sqlite_to_pg.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SQLITE_PATH = Path("data/run_history.db")

# テーブル名 → コピー対象 column のセット。 PG schema にあわせて選択。
TABLES_TO_COPY = [
    "runs",
    "articles",
    "run_logs",
    "article_entities",
    "dedup_seen_urls",
    "article_embeddings",
    "ops_notify_log",
    "f1_selections",
    "status_synthesis",
    "editorial_stance_reviews",
    "taxonomy_review_proposals",
    "pir_spotlight",
]


def main() -> int:
    import os

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set")
        return 1
    if not SQLITE_PATH.exists():
        print(f"ERROR: {SQLITE_PATH} not found")
        return 1

    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row

    pg_conn = psycopg.connect(dsn, row_factory=dict_row)

    try:
        for table in TABLES_TO_COPY:
            # SQLite に table 存在チェック
            try:
                cur = sqlite_conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}",  # noqa: S608
                )
                src_count = cur.fetchone()["n"]
            except sqlite3.OperationalError as e:
                print(f"[{table}] SQLite に存在せず skip: {e}")
                continue

            if src_count == 0:
                print(f"[{table}] 0 rows in SQLite, skip")
                continue

            # SQLite の column 一覧
            cur = sqlite_conn.execute(f"PRAGMA table_info({table})")
            sqlite_cols = [row["name"] for row in cur.fetchall()]

            # PG の column 一覧
            with pg_conn.cursor() as pc:
                pc.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = %s AND table_schema = 'public'
                    """,
                    (table,),
                )
                pg_cols = [row["column_name"] for row in pc.fetchall()]

            # 両方に存在する column のみ copy
            common_cols = [c for c in sqlite_cols if c in pg_cols]
            if not common_cols:
                print(f"[{table}] 共通 column なし、 skip")
                continue

            cols_sql = ", ".join(common_cols)
            placeholders = ", ".join(["%s"] * len(common_cols))

            # SELECT from SQLite
            sql_select = f"SELECT {cols_sql} FROM {table}"  # noqa: S608
            cur = sqlite_conn.execute(sql_select)
            rows = cur.fetchall()

            # INSERT to PG (ON CONFLICT DO NOTHING)
            inserted = 0
            with pg_conn.cursor() as pc:
                for row in rows:
                    values = tuple(row[c] for c in common_cols)
                    try:
                        pc.execute(
                            f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders}) "  # noqa: S608
                            "ON CONFLICT DO NOTHING",
                            values,
                        )
                        inserted += pc.rowcount
                    except Exception as e:  # noqa: BLE001
                        print(f"  [{table}] row insert failed: {e}")
            pg_conn.commit()
            print(f"[{table}] SQLite={src_count}, PG inserted={inserted}, common_cols={len(common_cols)}")

    finally:
        sqlite_conn.close()
        pg_conn.close()
    print("\nMigration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
