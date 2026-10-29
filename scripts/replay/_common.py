"""replay の共通部品の入口 (RP-1)。

- 読み取り専用 DB 接続 (書き込み文は送る前に拒否 + セッションを read-only に)
- 事象時刻の分割 (EVENT_TS_EXPR)
- 他モジュール (関門・R18・統計・manifest・ロック) の再エクスポート
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from scripts.replay._closure import (
    COS_THRESHOLD,
    R18_VERSION,
    SplitResult,
    assert_no_shared_component,
    build_components,
    resolve_roots,
    split_by_event_time,
)
from scripts.replay._gate import (
    GateRefusedError,
    JudgeSet,
    PreregRecord,
    TaintedRegistry,
    identify_judge_set,
    load_tainted_registry,
    require_prereg,
    sha256_file,
)
from scripts.replay._lock import AdjudicationLockBusyError, adjudication_lock
from scripts.replay._manifest import BodyLeakError, Manifest, assert_no_body, write_manifest
from scripts.replay._stats import (
    Interval,
    Observation,
    cluster_bootstrap_ci,
    discordant_counts,
    mcnemar_exact,
    rule_of_three,
    weighted_estimate,
    wilson_interval,
)
from src.storage.event_time import EVENT_TS_EXPR

__all__ = [
    "COS_THRESHOLD",
    "R18_VERSION",
    "AdjudicationLockBusyError",
    "BodyLeakError",
    "GateRefusedError",
    "Interval",
    "JudgeSet",
    "Manifest",
    "Observation",
    "PreregRecord",
    "ReadOnlyConnection",
    "ReadOnlyViolationError",
    "SplitResult",
    "TaintedRegistry",
    "adjudication_lock",
    "assert_no_body",
    "assert_no_shared_component",
    "build_components",
    "cluster_bootstrap_ci",
    "discordant_counts",
    "event_ts_sql",
    "identify_judge_set",
    "load_tainted_registry",
    "mcnemar_exact",
    "model_tiers_version",
    "open_readonly",
    "open_readonly_sqlite",
    "require_prereg",
    "resolve_roots",
    "rule_of_three",
    "sha256_file",
    "split_by_event_time",
    "weighted_estimate",
    "wilson_interval",
    "write_manifest",
]

_READ_ONLY_RE = re.compile(r"^\s*\(?\s*(select|with|show|explain)\b", re.IGNORECASE)
_WRITE_WORD_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum"
    r"|reindex|set|reset|call|do|lock|merge)\b",
    re.IGNORECASE,
)
_STRING_RE = re.compile(r"'(?:[^']|'')*'")


class ReadOnlyViolationError(RuntimeError):
    """書き込み (になりうる) 文を拒否した。"""


def _assert_read_only_sql(sql: str) -> None:
    stripped = _STRING_RE.sub("''", sql)
    if ";" in stripped.strip().rstrip(";"):
        raise ReadOnlyViolationError("multiple statements are refused")
    if not _READ_ONLY_RE.match(stripped):
        raise ReadOnlyViolationError("only SELECT/WITH/SHOW/EXPLAIN are allowed")
    # CTE の中の書き込み (WITH ... DELETE ...) / SELECT ... FOR UPDATE も拒否する。
    # 列名・別名に update 等を含む場合の誤拒否は許容 (replay は単純な SELECT で足りる)。
    if _WRITE_WORD_RE.search(re.sub(r'"[^"]*"', '""', stripped)):
        raise ReadOnlyViolationError("write keyword found in statement")


class ReadOnlyConnection:
    """DB-API 接続の薄い包み。execute は読み取り文だけ通す。"""

    def __init__(self, conn: Any, *, placeholder: str = "?") -> None:
        self._conn = conn
        self._placeholder = placeholder

    def execute(self, sql: str, params: Any = ()) -> Any:
        """sql は "?" プレースホルダで書く (PG では %s に置き換える)。"""
        _assert_read_only_sql(sql)
        if self._placeholder != "?":
            sql = sql.replace("?", self._placeholder)
        return self._conn.execute(sql, params)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ReadOnlyConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_readonly(database_url: str) -> ReadOnlyConnection:
    """PostgreSQL へ read-only セッションで接続する (default_transaction_read_only = on)。"""
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(database_url, autocommit=True, row_factory=dict_row)
    conn.execute("SET default_transaction_read_only = on")
    return ReadOnlyConnection(conn, placeholder="%s")


def open_readonly_sqlite(path: str) -> ReadOnlyConnection:
    """SQLite (dev/tests) を mode=ro で開く。"""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return ReadOnlyConnection(conn)


def event_ts_sql(alias: str = "a") -> str:
    """事象時刻の SQL 式 (EVENT_TS_EXPR の SSoT をそのまま使う)。"""
    return EVENT_TS_EXPR.format(a=alias)


def model_tiers_version(conn: ReadOnlyConnection) -> int | None:
    """model_tiers の現行版番号 (read-only SELECT)。未保存なら None。"""
    row = conn.execute(
        "SELECT MAX(version) AS v FROM app_config_versions WHERE config_key = ?",
        ("model_tiers",),
    ).fetchone()
    if row is None:
        return None
    value = row["v"]
    return int(value) if value is not None else None
