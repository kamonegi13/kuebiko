"""運用 config の DB バックエンド store。

routing rules / source_quality / PIR 等、**運用中に UI から編集し履歴が要る config** を
DB (``app_config_versions``) に保存する。yaml/git を runtime の正にしない
(アプリが git を触る anti-pattern の解消)。

- 保存ごとに version 行を append → 履歴が自然に貯まる (任意世代へ revert 可)。
- 現在値 = config_key ごとの最大 version 行の value_json (JSON parse)。
- DB が空の key は ``seed_config_if_absent`` で yaml seed から初回取り込み (起動時)。
- dual backend: ``db_backend.connect`` (PG/SQLite)。DDL の AUTOINCREMENT / ``?`` placeholder は
  ``_PgCursor.execute`` の ``translate_sql`` が吸収するので self-contained に table 化できる。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.logging_config import get_logger
from src.storage.db_backend import connect as _backend_connect

_log = get_logger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS app_config_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    config_key  TEXT    NOT NULL,
    value_json  TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_config_key_version
    ON app_config_versions(config_key, version);
"""


@dataclass(frozen=True)
class ConfigVersion:
    """1 件の保存履歴 (revert / 監査用)。"""

    version: int
    note: str
    created_at: str


_PG_READ_ONLY_SQLSTATE = "25006"  # read_only_sql_transaction
_SQLITE_READ_ONLY_MARKER = "readonly database"


def is_read_only_error(exc: BaseException) -> bool:
    """読み取り専用の接続に書き込み (DDL) を出して拒否された例外か。

    PG は ``ReadOnlySqlTransaction`` (SQLSTATE 25006)、sqlite は
    ``attempt to write a readonly database``。それ以外 (接続不能・権限・構文等) は False。
    """
    if getattr(exc, "sqlstate", None) == _PG_READ_ONLY_SQLSTATE:
        return True
    if type(exc).__name__ == "ReadOnlySqlTransaction":
        return True
    return isinstance(exc, sqlite3.OperationalError) and _SQLITE_READ_ONLY_MARKER in str(exc)


def _open(db_path: Path | None) -> Any:
    conn = _backend_connect(db_path)
    if hasattr(conn, "row_factory"):
        conn.row_factory = sqlite3.Row  # PG wrapper では no-op (dict_row)
    try:
        conn.executescript(_DDL)  # CREATE TABLE/INDEX IF NOT EXISTS (idempotent)
    except Exception as e:
        # 読み取り専用の接続 (測定の接続等) は DDL を拒否される。表は既にある前提で
        # 続行し、後続の SELECT に任せる (表が無ければそこで大きく落ちる)。
        # 書き込み (save_config の INSERT) は読み取り専用なら従来どおり失敗する。
        # それ以外の例外は握らない。
        if not is_read_only_error(e):
            conn.close()
            raise
        _log.debug("config_store_ddl_skipped_read_only")
        _rollback_quietly(conn)
    return conn


def _rollback_quietly(conn: Any) -> None:
    """失敗した文の後で接続を使える状態に戻す (PG は失敗した文でトランザクションが中断する)。"""
    try:
        conn.rollback()
    except Exception as e:  # noqa: BLE001 — 戻せなくても SELECT 側で表面化する
        _log.debug("config_store_rollback_failed", error=type(e).__name__)


def get_config(key: str, *, db_path: Path | None = None) -> Any | None:
    """key の現在値 (最大 version) を返す。未保存なら None。"""
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT value_json FROM app_config_versions "
            "WHERE config_key=? ORDER BY version DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["value_json"])
    finally:
        conn.close()


def save_config(key: str, value: Any, *, note: str = "", db_path: Path | None = None) -> int:
    """value を新 version として保存。返り値 = 新 version 番号。"""
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(version) AS mx FROM app_config_versions WHERE config_key=?",
            (key,),
        ).fetchone()
        mx = row["mx"] if row is not None and row["mx"] is not None else 0
        version = int(mx) + 1
        conn.execute(
            "INSERT INTO app_config_versions "
            "(config_key, value_json, version, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                key,
                json.dumps(value, ensure_ascii=False),
                version,
                note,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
        return version
    finally:
        conn.close()


def seed_config_if_absent(
    key: str,
    value: Any,
    *,
    note: str = "seed (yaml import)",
    db_path: Path | None = None,
) -> bool:
    """key が未保存なら value を version 1 として取り込む。取り込んだら True。"""
    if get_config(key, db_path=db_path) is not None:
        return False
    save_config(key, value, note=note, db_path=db_path)
    _log.info("app_config_seeded", key=key)
    return True


def list_history(key: str, *, limit: int = 50, db_path: Path | None = None) -> list[ConfigVersion]:
    """保存履歴 (新しい順)。"""
    conn = _open(db_path)
    try:
        rows = conn.execute(
            "SELECT version, note, created_at FROM app_config_versions "
            "WHERE config_key=? ORDER BY version DESC LIMIT ?",
            (key, limit),
        ).fetchall()
        return [
            ConfigVersion(
                version=int(r["version"]),
                note=r["note"] or "",
                created_at=str(r["created_at"]),
            )
            for r in rows
        ]
    finally:
        conn.close()


def get_config_version(key: str, version: int, *, db_path: Path | None = None) -> Any | None:
    """特定 version の値を返す (revert 用)。"""
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT value_json FROM app_config_versions WHERE config_key=? AND version=?",
            (key, version),
        ).fetchone()
        return json.loads(row["value_json"]) if row is not None else None
    finally:
        conn.close()
