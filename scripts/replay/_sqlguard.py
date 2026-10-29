"""読み取り専用 SQL の検査とプレースホルダ変換 (文字列リテラルを区別する)。"""

from __future__ import annotations

import re

_READ_ONLY_RE = re.compile(r"^\s*\(?\s*(select|with|show|explain)\b", re.IGNORECASE)
_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum"
    r"|reindex|set|reset|call|do|lock|merge|into|set_config|nextval|setval"
    r"|dblink\w*|lo_\w+|pg_advisory\w*|pg_terminate\w*|pg_cancel\w*|pg_read\w*"
    r"|pg_write\w*|pg_reload\w*|pg_switch\w*|pg_notify|listen|notify|prepare|execute"
    r"|analyze)\b",
    re.IGNORECASE,
)
_LITERAL_RE = re.compile(r"'(?:[^']|'')*'|\"[^\"]*\"")
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


class ReadOnlyViolationError(RuntimeError):
    """書き込み (になりうる) 文を拒否した。"""


def mask_literals(sql: str) -> str:
    """コメントを除き、文字列リテラルと引用識別子を空にする。"""
    return _LITERAL_RE.sub("''", _COMMENT_RE.sub(" ", sql))


def assert_read_only_sql(sql: str) -> None:
    masked = mask_literals(sql)
    if "$" in masked:
        raise ReadOnlyViolationError("dollar-quoting / $n parameters are refused")
    if ";" in masked.strip().rstrip(";"):
        raise ReadOnlyViolationError(
            "semicolon inside a statement is refused (put it in a bound parameter, not the SQL)"
        )
    if not _READ_ONLY_RE.match(masked):
        raise ReadOnlyViolationError("only SELECT/WITH/SHOW/EXPLAIN are allowed")
    found = _FORBIDDEN_RE.search(masked)
    if found:
        raise ReadOnlyViolationError(f"forbidden keyword/function in statement: {found.group(0)}")


def to_psycopg_placeholders(sql: str) -> str:
    """ "?" を %s に、リテラル外の "%" を %% にする。リテラル内の "?" は触らず "%" だけ倍にする。"""
    out: list[str] = []
    pos = 0
    for m in _LITERAL_RE.finditer(sql):
        out.append(sql[pos : m.start()].replace("%", "%%").replace("?", "%s"))
        out.append(m.group(0).replace("%", "%%"))
        pos = m.end()
    out.append(sql[pos:].replace("%", "%%").replace("?", "%s"))
    return "".join(out)
