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
_WORD_RE = re.compile(r"[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_$\u0080-\U0010ffff]*")
_PLAIN_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_$]*$")
_CALL_RE = re.compile(r"([a-z_][a-z0-9_$]*(?:\s*\.\s*[a-z_][a-z0-9_$]*)*)\s*\(")
_OPAQUE_IDENT = "__quoted__"


class ReadOnlyViolationError(RuntimeError):
    """書き込み (になりうる) 文を拒否した。"""


def _scan_quoted(sql: str, start: int, quote: str, *, backslash: bool) -> int:
    """sql[start] が開き引用符。閉じ引用符の次の位置を返す (未終了は拒否)。"""
    i = start + 1
    while i < len(sql):
        ch = sql[i]
        if backslash and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if sql[i + 1 : i + 2] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    raise ReadOnlyViolationError("unterminated quoted literal/identifier")


def normalize_sql(sql: str) -> str:
    """左から 1 回の字句解析。コメント・$ 引用・U& は拒否し、リテラルは空にし、
    引用識別子は引用を外して小文字にする。E'...' は backslash エスケープを解釈する。"""
    out: list[str] = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        two = sql[i : i + 2]
        if two in ("--", "/*"):
            raise ReadOnlyViolationError("SQL comments are refused")
        if ch == "$":
            raise ReadOnlyViolationError("dollar-quoting / $n parameters are refused")
        if ch == "'":
            i = _scan_quoted(sql, i, "'", backslash=False)
            out.append("''")
        elif ch == '"':
            end = _scan_quoted(sql, i, '"', backslash=False)
            inner = sql[i + 1 : end - 1].replace('""', '"').lower()
            out.append(inner if _PLAIN_IDENT_RE.match(inner) else _OPAQUE_IDENT)
            i = end
        else:
            m = _WORD_RE.match(sql, i)
            if not m:
                out.append(ch)
                i += 1
                continue
            word, i = m.group(0).lower(), m.end()
            nxt = sql[i : i + 1]
            if word == "u" and sql[i : i + 2] in ("&'", '&"'):
                raise ReadOnlyViolationError("U& escapes are refused")
            if word == "e" and nxt == "'":
                i = _scan_quoted(sql, i, "'", backslash=True)
                out.append("''")
            else:
                out.append(word)
    return "".join(out)


def _assert_functions_allowed(normalized: str) -> None:
    for match in _CALL_RE.finditer(normalized):
        name = re.split(r"\s*\.\s*", match.group(1))[-1]
        if name in ALLOWED_FUNCTIONS or name in _GRAMMAR_WORDS:
            continue
        raise ReadOnlyViolationError(f"function/call not in the allowlist: {name}")


def assert_read_only_sql(sql: str) -> None:
    normalized = normalize_sql(sql)
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


def _literal_spans(sql: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(sql):
        if sql[i] in "'\"":
            is_e = (
                sql[i] == "'"
                and sql[i - 1 : i] in ("e", "E")
                and not (i >= 2 and (sql[i - 2].isalnum() or sql[i - 2] in "_$"))
            )
            end = _scan_quoted(sql, i, sql[i], backslash=is_e)
            spans.append((i, end))
            i = end
        else:
            i += 1
    return spans


def to_psycopg_placeholders(sql: str) -> str:
    """ "?" を %s に、リテラル外の "%" を %% にする。リテラル内の "?" は触らず "%" だけ倍にする。"""
    out: list[str] = []
    pos = 0
    for start, end in _literal_spans(sql):
        out.append(sql[pos:start].replace("%", "%%").replace("?", "%s"))
        out.append(sql[start:end].replace("%", "%%"))
        pos = end
    out.append(sql[pos:].replace("%", "%%").replace("?", "%s"))
    return "".join(out)
