"""SELECT 専用スクリプトの DB 接続を読み取り専用にする (PG のとき)。

接続は autocommit のため、文ごとのトランザクションに ``SET TRANSACTION READ ONLY`` を
掛けても次の文に効かない。セッションの既定 (``default_transaction_read_only``) を on にして、
以後の全文を読み取り専用にする。SQLite (dev / tests) は対象外。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def open_read_only(connect: Callable[..., Any], db_path: Path | str | None) -> Any:
    """``connect`` で開いた接続を、PG なら読み取り専用のセッションにして返す。"""
    from src.storage.db_backend import is_pg_enabled

    conn = connect(db_path)
    if is_pg_enabled():
        try:
            conn.execute("SET default_transaction_read_only = on")
        except Exception:
            conn.close()
            raise
    return conn
