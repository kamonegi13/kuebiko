"""replay の共通部品の入口 (RP-1)。

- 読み取り専用 DB 接続 (書き込み文は送る前に拒否 + セッションを read-only に)
- 事象時刻の分割 (EVENT_TS_EXPR)
- 他モジュール (関門・R18・統計・manifest・ロック) の再エクスポート
"""

from __future__ import annotations

import os
import sqlite3
import warnings
from typing import Any

from scripts.replay._closure import (
    COS_THRESHOLD,
    R18_VERSION,
    ComponentTooLargeError,
    SplitResult,
    assert_components_ok,
    assert_no_shared_component,
    build_components,
    component_diagnostics,
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
from scripts.replay._sqlguard import (
    ReadOnlyViolationError,
    assert_read_only_sql,
    to_psycopg_placeholders,
)
from scripts.replay._stats import (
    Interval,
    Observation,
    cluster_bootstrap_ci,
    discordant_counts,
    mcnemar_exact,
    rule_of_three,
    rule_of_three_exact,
    weighted_estimate,
    wilson_interval,
)
from src.storage.event_time import EVENT_TS_EXPR

__all__ = [
    "COS_THRESHOLD",
    "R18_VERSION",
    "AdjudicationLockBusyError",
    "BodyLeakError",
    "ComponentTooLargeError",
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
    "assert_components_ok",
    "assert_no_body",
    "assert_no_shared_component",
    "assert_read_only_sql",
    "build_components",
    "cluster_bootstrap_ci",
    "component_diagnostics",
    "discordant_counts",
    "event_ts_sql",
    "identify_judge_set",
    "load_tainted_registry",
    "mcnemar_exact",
    "model_tiers_version",
    "open_readonly",
    "open_readonly_sqlite",
    "require_prereg",
    "resolve_database_url",
    "resolve_roots",
    "rule_of_three",
    "rule_of_three_exact",
    "sha256_file",
    "split_by_event_time",
    "to_psycopg_placeholders",
    "weighted_estimate",
    "wilson_interval",
    "write_manifest",
]


class ReadOnlyConnection:
    """DB-API 接続の薄い包み。execute は読み取り文だけ通す。"""

    def __init__(self, conn: Any, *, placeholder: str = "?") -> None:
        self._conn = conn
        self._placeholder = placeholder

    def execute(self, sql: str, params: Any = ()) -> Any:
        """sql は "?" プレースホルダで書く (PG では %s に置き換える)。"""
        assert_read_only_sql(sql)
        if self._placeholder != "?":
            sql = to_psycopg_placeholders(sql)
        return self._conn.execute(sql, params)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ReadOnlyConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


REPLAY_URL_ENV = "REPLAY_DATABASE_URL"


def resolve_database_url(explicit: str | None = None) -> str:
    """接続先: 明示 > REPLAY_DATABASE_URL (専用の read-only ロール) > DATABASE_URL (警告)。"""
    if explicit:
        return explicit
    dedicated = os.environ.get(REPLAY_URL_ENV)
    if dedicated:
        return dedicated
    fallback = os.environ.get("DATABASE_URL")
    if not fallback:
        raise ReadOnlyViolationError(f"{REPLAY_URL_ENV} is not set")
    warnings.warn(
        f"{REPLAY_URL_ENV} not set; falling back to DATABASE_URL (not a dedicated read-only role)",
        stacklevel=2,
    )
    return fallback


def open_readonly(database_url: str | None = None) -> ReadOnlyConnection:
    """PostgreSQL へ read-only セッションで接続する (default_transaction_read_only = on)。

    専用ロールは scripts/replay/create_readonly_role.sql (CONNECT + USAGE + SELECT のみ)。
    """
    database_url = resolve_database_url(database_url)
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(
        database_url,
        autocommit=True,
        row_factory=dict_row,
        options="-c default_transaction_read_only=on",
    )
    conn.read_only = True
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
