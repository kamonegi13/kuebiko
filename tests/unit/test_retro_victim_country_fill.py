"""scripts/retro_victim_country_fill.py の dry-run / apply / 退避のテスト (SQLite 上)。"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "retro_victim_country_fill.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("retro_victim_country_fill", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY, article_id TEXT, status TEXT, article_type TEXT,
            category TEXT, title TEXT, summary TEXT, victim_country_iso TEXT,
            victim_country_raw TEXT, victim_country_scope TEXT, created_at TEXT
        );
        CREATE TABLE article_entities (article_id TEXT, entity_type TEXT, value TEXT);
        """
    )
    now = datetime.now(UTC).isoformat()
    rows = [
        (
            1,
            "a1",
            "posted",
            "breaking",
            "breach",
            "架空商事株式会社に不正アクセス",
            "要約",
            None,
            None,
            None,
            now,
        ),
        (
            2,
            "a2",
            "posted",
            "breaking",
            "breach",
            "Unknown victims posted",
            "x",
            None,
            None,
            None,
            now,
        ),
        (
            3,
            "a3",
            "posted",
            "breaking",
            "breach",
            "架空商事株式会社に不正アクセス",
            "要約",
            "TW",
            "Taiwan",
            None,
            now,
        ),
        (
            4,
            "a4",
            "posted",
            "research",
            "breach",
            "架空商事株式会社に不正アクセス",
            "要約",
            None,
            None,
            None,
            now,
        ),
    ]
    con.executemany("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("INSERT INTO article_entities VALUES ('a1','victim_org','架空商事株式会社')")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, sqlite3.Connection]:
    module = _load_script()
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _seed(connection)
    monkeypatch.setattr(module, "connect", lambda: _NoCloseConnection(connection))
    monkeypatch.setattr(module, "load_org_history", dict)
    return module, connection


class _NoCloseConnection:
    """script の ``conn.close()`` で :memory: を失わないための薄いラッパ。"""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner

    def cursor(self) -> sqlite3.Cursor:
        return self._inner.cursor()

    def commit(self) -> None:
        self._inner.commit()

    def close(self) -> None:
        return None


def test_dry_run_changes_nothing(env: tuple[ModuleType, sqlite3.Connection]) -> None:
    script, con = env

    code = script.main(apply=False, days=30)

    assert code == 0
    row = con.execute("SELECT victim_country_iso FROM articles WHERE id=1").fetchone()
    assert row[0] is None


def test_apply_updates_only_fillable_rows_and_backs_up(
    env: tuple[ModuleType, sqlite3.Connection],
) -> None:
    script, con = env

    code = script.main(apply=True, days=30)

    assert code == 0
    rows = {
        r["id"]: (r["victim_country_iso"], r["victim_country_raw"])
        for r in con.execute("SELECT * FROM articles")
    }
    assert rows[1] == ("JP", "inferred:jp_legal_form")
    assert rows[2] == (None, None)  # 規則に当たらない
    assert rows[3] == ("TW", "Taiwan")  # LLM の値には触れない
    assert rows[4] == (None, None)  # 速報以外は対象外
    backups = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '_backup_victim_country_fill_%'"
        )
    ]
    assert len(backups) == 1
    saved = con.execute(f"SELECT id, victim_country_iso FROM {backups[0]}").fetchall()  # noqa: S608
    assert [tuple(r) for r in saved] == [(1, None)]
