"""scripts/replay/_sqlguard の敵対的入力テスト (コメント・リテラル・E'' の字句解析)。"""

from __future__ import annotations

import pytest

from scripts.replay._sqlguard import (
    ReadOnlyViolationError,
    assert_read_only_sql,
    to_psycopg_placeholders,
)

REJECTED = {
    "line comment after literal": "SELECT '--', set_config('x','off',false) --'",
    "block comment in literal": "SELECT '/*', pg_sleep(5), '*/'",
    "E-string backslash quote": "SELECT E'\\'' || set_config('x.y','1',false) || E'\\''",
    "quote char in literal": "SELECT '\"', pg_read_file('/etc/passwd'), '\"'",
    "U& identifier": "SELECT U&\"\\0073et_config\"('a','b',false)",
    "schema-qualified quoted": "SELECT pg_catalog.\"set_config\"('a','b',false)",
    "lo_import": "SELECT lo_import('/etc/passwd')",
    "select into": "SELECT 1 INTO t",
    "for update": "SELECT * FROM articles FOR UPDATE",
    "data-modifying CTE": "WITH x AS (DELETE FROM articles RETURNING 1) SELECT 1",
    "explain analyze": "EXPLAIN ANALYZE SELECT 1",
    "regexp via repeat": "SELECT regexp_replace(repeat('a',30)||'b','(a+)+$','')",
    "nested comment": "SELECT 1 /* /* */ , pg_sleep(1) */",
    "multi statement": "SELECT 1; DELETE FROM articles",
    "U& literal": "SELECT U&'\\0027', pg_sleep(1), U&'\\0027'",
    "dblink": "SELECT * FROM dblink('x','DELETE')",
    "E-string then current_setting": "SELECT E'\\\\', current_setting('x')",
    "CTE with comment tail": "WITH a AS (SELECT '--'), x AS (DELETE FROM t) SELECT 1 --'",
    "dollar quote": "SELECT $$x$$",
    "unterminated literal": "SELECT 'abc",
}
ACCEPTED = {
    "function name only inside a literal": "SELECT 'pg_sleep(5)'::text",
    "string_agg": "SELECT string_agg(title, ',') FROM articles",
    "E-string with escaped backslash": "SELECT E'\\\\' || 'x'",
    "generate_series (bound by statement_timeout)": "SELECT count(*) FROM generate_series(1, 10)",
}


@pytest.mark.parametrize("sql", list(REJECTED.values()), ids=list(REJECTED))
def test_guard_rejects_adversarial_query(sql: str) -> None:
    with pytest.raises(ReadOnlyViolationError):
        assert_read_only_sql(sql)


@pytest.mark.parametrize("sql", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_guard_accepts_benign_query(sql: str) -> None:
    assert_read_only_sql(sql)


def test_placeholders_follow_e_string_escapes() -> None:
    sql = "SELECT E'\\'?%' AS a, ? FROM t WHERE x LIKE '%?'"
    assert to_psycopg_placeholders(sql) == "SELECT E'\\'?%%' AS a, %s FROM t WHERE x LIKE '%%?'"
