"""既存行の original_title を dedup_seen_urls.title から埋める (2026-10-18)。

dedup_seen_urls.title は取込時の元の見出し (Article.title)。article_id で articles に結ぶ。
dedup_seen_urls は 2026-05 以降しか無く、それ以前の行は埋まらない (推測で埋めない)。
original_title IS NULL の行だけを対象にするので冪等。
"""

from __future__ import annotations

from typing import Any, Final

_SOURCE_TITLE: Final = (
    "SELECT d.title FROM dedup_seen_urls d"
    " WHERE d.article_id = articles.article_id AND d.title IS NOT NULL AND d.title <> ''"
    " ORDER BY d.first_seen ASC LIMIT 1"
)

#: 件数の確認 (dry-run)。更新対象 = original_title が NULL で、元の見出しを引ける行
COUNT_SQL: Final = (
    "SELECT COUNT(*) FROM articles WHERE original_title IS NULL"
    f" AND EXISTS ({_SOURCE_TITLE.replace('SELECT d.title', 'SELECT 1')})"
)

UPDATE_SQL: Final = (
    f"UPDATE articles SET original_title = ({_SOURCE_TITLE})"
    " WHERE original_title IS NULL"
    f" AND EXISTS ({_SOURCE_TITLE.replace('SELECT d.title', 'SELECT 1')})"
)


def count_backfillable(conn: Any) -> int:
    row = conn.execute(COUNT_SQL).fetchone()
    return int(row[0]) if row is not None else 0


def apply_backfill(conn: Any) -> int:
    """UPDATE を実行して更新した行数を返す (commit は呼び出し側)。"""
    cur = conn.execute(UPDATE_SQL)
    return int(cur.rowcount or 0)
