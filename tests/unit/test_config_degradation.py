"""運用 config の degrade は「値の問題」に限る、の関門。

運用 config は **DB (config_store) が SSoT で yaml は seed (出荷時の既定)**。
DB 読み取りの失敗を一律 warning にして seed へ落とすと、DB が落ちている間、
**利用者が編集したモデルティア・プロンプト・rubric・配信ルールではなく出荷時の
既定でパイプラインが走り、その結果が永久に保存される**。

2026-08-25 の接続プール枯渇では実際にこれが起きており、`pir_db_read_failed` /
`routing_rules_db_read_failed` / `channel_registry_db_load_failed` が warning に
埋もれて障害の切り分けを遅らせた。

値が無い/壊れている (= データの問題) なら seed で良い。DB に届いていない
(= 基盤の問題) なら degrade してはいけない。判定は
``src.storage.db_backend.raise_if_infrastructure`` に一本化する。
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3

import pytest

from src.storage.db_backend import is_infrastructure_error, raise_if_infrastructure


class TestClassification:
    def test_pool_exhaustion_is_infrastructure(self) -> None:
        psycopg_pool = pytest.importorskip("psycopg_pool")

        assert is_infrastructure_error(psycopg_pool.PoolTimeout("no connection"))

    def test_connection_failure_is_infrastructure(self) -> None:
        psycopg = pytest.importorskip("psycopg")

        assert is_infrastructure_error(psycopg.OperationalError("connection refused"))

    def test_sqlite_lock_is_infrastructure(self) -> None:
        assert is_infrastructure_error(sqlite3.OperationalError("database is locked"))

    def test_value_problems_are_not_infrastructure(self) -> None:
        """値の不在・破損は seed へ degrade してよい (従来の意図)。"""
        assert not is_infrastructure_error(ValueError("bad yaml"))
        assert not is_infrastructure_error(KeyError("missing"))
        assert not is_infrastructure_error(TypeError("wrong shape"))

    def test_raise_if_infrastructure_reraises_only_infrastructure(self) -> None:
        with pytest.raises(sqlite3.OperationalError):
            raise_if_infrastructure(sqlite3.OperationalError("locked"), context="t")

        raise_if_infrastructure(ValueError("bad"), context="t")  # 戻ってくれば degrade 可


def _unguarded_sites() -> list[str]:
    """``get_config()`` を包む except で ``raise_if_infrastructure`` を通していない箇所。"""
    out: list[str] = []
    for path in sorted(pathlib.Path("src").rglob("*.py")):
        src = path.read_text()
        if "get_config(" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Try):
                continue
            if "get_config(" not in (ast.get_source_segment(src, node) or ""):
                continue
            for handler in node.handlers:
                body = ast.get_source_segment(src, handler) or ""
                degrades = "_log.warning" in body or body.strip().endswith("pass")
                if degrades and "raise_if_infrastructure" not in body:
                    out.append(f"{path}:{handler.lineno}")
    return out


def test_every_config_degradation_checks_for_infrastructure_failure() -> None:
    """落ちたら except の先頭に ``raise_if_infrastructure(e, context=...)`` を足す。"""
    unguarded = _unguarded_sites()
    assert unguarded == [], (
        "DB 未達でも出荷時の既定へ degrade してしまう箇所がある:\n  " + "\n  ".join(unguarded)
    )
