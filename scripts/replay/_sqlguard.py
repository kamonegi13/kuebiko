"""読み取り専用 SQL の検査 (関数名の allowlist 方式) とプレースホルダ変換。

防御は 3 重: (1) 専用の read-only ロール (create_readonly_role.sql)、(2) セッションの
read-only 設定、(3) この検査。この検査は「許可した関数だけ」を通す。引用識別子
("set_config" や pg_catalog."set_config") は引用を外し、スキーマ修飾を落としてから照合する。
限界: CTE / 別名の列リスト (`WITH t(a, b) AS`、`AS t(a, b)`) は関数呼び出しと区別できず拒否する。
"""

from __future__ import annotations

import re

ALLOWED_FUNCTIONS = frozenset(
    {
        # 集約・窓
        "count", "sum", "avg", "min", "max", "bool_and", "bool_or", "stddev", "variance",
        "array_agg", "string_agg", "percentile_cont", "percentile_disc", "row_number",
        "rank", "dense_rank", "lag", "lead", "first_value", "last_value", "ntile",
        # スカラ
        "coalesce", "nullif", "greatest", "least", "length", "char_length", "substr",
        "substring", "lower", "upper", "trim", "ltrim", "rtrim", "replace", "concat",
        "position", "split_part", "left", "right", "md5", "regexp_replace", "regexp_match",
        "abs", "round", "floor", "ceil", "ceiling", "power", "sqrt", "ln", "log", "exp",
        "mod", "sign", "cast", "extract", "date_trunc", "date_part", "now", "age",
        "to_char", "to_timestamp", "make_interval", "date",
        # JSON・配列・集合を返す純粋関数
        "json_extract_path_text", "jsonb_extract_path_text", "json_array_length",
        "jsonb_array_length", "json_typeof", "jsonb_typeof", "array_length", "cardinality",
        "unnest", "generate_series",
        # 型名 (varchar(10) 等)
        "varchar", "char", "numeric", "decimal", "timestamp", "timestamptz", "interval",
        "time", "bit", "float", "int",
    }
)  # fmt: skip
# 文法上 "(" の前に来る語 (関数ではない)。
_GRAMMAR_WORDS = frozenset(
    {
        "in", "not", "exists", "any", "all", "some", "values", "over", "filter", "and",
        "or", "on", "using", "as", "from", "join", "select", "where", "when", "then",
        "else", "case", "by", "partition", "order", "between", "like", "ilike", "similar",
        "limit", "offset", "union", "intersect", "except", "having", "group", "distinct",
        "with", "within", "array", "row", "materialized", "lateral", "is", "end", "rows",
        "range", "recursive", "inner", "left", "right", "outer", "cross", "natural",
        "full", "to", "escape", "at", "only", "ordinality", "show", "explain",
    }
)  # fmt: skip
_READ_ONLY_RE = re.compile(r"^\s*\(?\s*(select|with|show|explain)\b", re.IGNORECASE)
_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum"
    r"|reindex|set|reset|call|do|lock|merge|into|listen|notify|prepare|execute|analyze"
    r"|for\s+(?:no\s+key\s+update|update|share|key\s+share))\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_PLAIN_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_$]*$")
_CALL_RE = re.compile(r"([a-z_][a-z0-9_$]*(?:\s*\.\s*[a-z_][a-z0-9_$]*)*)\s*\(")
_OPAQUE_IDENT = "__quoted__"


class ReadOnlyViolationError(RuntimeError):
    """書き込み (になりうる) 文を拒否した。"""


def _normalize_token(match: re.Match[str]) -> str:
    token = match.group(0)
    if token.startswith("'"):
        return "''"
    inner = token[1:-1].replace('""', '"').lower()
    return inner if _PLAIN_IDENT_RE.match(inner) else _OPAQUE_IDENT


def normalize_sql(sql: str) -> str:
    """コメントを除き、文字列リテラルを空にし、引用識別子の引用を外して小文字にする。"""
    return _TOKEN_RE.sub(_normalize_token, _COMMENT_RE.sub(" ", sql)).lower()


def _assert_functions_allowed(normalized: str) -> None:
    for match in _CALL_RE.finditer(normalized):
        name = re.split(r"\s*\.\s*", match.group(1))[-1]
        if name in ALLOWED_FUNCTIONS or name in _GRAMMAR_WORDS:
            continue
        raise ReadOnlyViolationError(f"function/call not in the allowlist: {name}")


def assert_read_only_sql(sql: str) -> None:
    normalized = normalize_sql(sql)
    if "$" in normalized:
        raise ReadOnlyViolationError("dollar-quoting / $n parameters are refused")
    if ";" in normalized.strip().rstrip(";"):
        raise ReadOnlyViolationError(
            "semicolon inside a statement is refused (put it in a bound parameter, not the SQL)"
        )
    if not _READ_ONLY_RE.match(normalized):
        raise ReadOnlyViolationError("only SELECT/WITH/SHOW/EXPLAIN are allowed")
    found = _FORBIDDEN_RE.search(normalized)
    if found:
        raise ReadOnlyViolationError(f"forbidden keyword in statement: {found.group(0)}")
    _assert_functions_allowed(normalized)


def to_psycopg_placeholders(sql: str) -> str:
    """ "?" を %s に、リテラル外の "%" を %% にする。リテラル内の "?" は触らず "%" だけ倍にする。"""
    out: list[str] = []
    pos = 0
    for m in _TOKEN_RE.finditer(sql):
        out.append(sql[pos : m.start()].replace("%", "%%").replace("?", "%s"))
        out.append(m.group(0).replace("%", "%%"))
        pos = m.end()
    out.append(sql[pos:].replace("%", "%%").replace("?", "%s"))
    return "".join(out)
