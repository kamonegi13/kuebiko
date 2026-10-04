"""SQL の重要度レベル式 (src/storage/importance_level_sql.py) と Python の
``importance_level()`` (src/cti/importance_v2.py) が一致することを固定する。

SSoT は Python 側。SQL は ORDER BY に埋め込むための**鏡写し**なので、どちらかの
決まりごとを変えたときに食い違っていないかをここで検証する (全 6 通り + None)。
"""

from __future__ import annotations

import sqlite3
from typing import cast

import pytest

from src.cti.importance_v2 import Severity, importance_level
from src.storage.importance_level_sql import level_scalar_subquery

_SEVERITIES: tuple[Severity | None, ...] = ("S3", "S2", "S1", None)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE articles (article_id TEXT PRIMARY KEY)",
    )
    c.execute(
        "CREATE TABLE article_importance_v2 ("
        " article_id TEXT PRIMARY KEY, severity TEXT, relevant INTEGER NOT NULL)",
    )
    return c


def _level_via_sql(
    conn: sqlite3.Connection, severity: Severity | None, relevant: bool
) -> int | None:
    conn.execute("DELETE FROM articles")
    conn.execute("DELETE FROM article_importance_v2")
    conn.execute("INSERT INTO articles (article_id) VALUES ('a1')")
    conn.execute(
        "INSERT INTO article_importance_v2 (article_id, severity, relevant) VALUES (?, ?, ?)",
        ("a1", severity, 1 if relevant else 0),
    )
    expr = level_scalar_subquery("articles.article_id")
    row = conn.execute(f"SELECT {expr} FROM articles WHERE article_id = 'a1'").fetchone()  # noqa: S608
    return cast("int | None", row[0])


@pytest.mark.parametrize("severity", _SEVERITIES)
@pytest.mark.parametrize("relevant", [True, False])
def test_sql_case_matches_python_function(
    conn: sqlite3.Connection, severity: Severity | None, relevant: bool
) -> None:
    assert _level_via_sql(conn, severity, relevant) == importance_level(severity, relevant)


def test_unrecorded_article_has_null_level(conn: sqlite3.Connection) -> None:
    """記録の無い記事 (article_importance_v2 に行が無い) は None (相関サブクエリが空)。"""
    conn.execute("INSERT INTO articles (article_id) VALUES ('a1')")
    expr = level_scalar_subquery("articles.article_id")
    row = conn.execute(f"SELECT {expr} FROM articles WHERE article_id = 'a1'").fetchone()  # noqa: S608
    assert row[0] is None


def test_all_six_levels_and_none_are_distinct() -> None:
    """6 段階 + None が実際に全部出る (決まりごとの退行を検知する足場)。"""
    severities: tuple[Severity, ...] = ("S3", "S2", "S1")
    levels = {importance_level(sev, rel) for sev in severities for rel in (True, False)}
    levels.add(importance_level(None, True))
    assert levels == {1, 2, 3, 4, 5, 6, None}
