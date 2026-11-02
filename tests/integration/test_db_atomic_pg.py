"""``atomic()`` と、それを使う書き込みが実 PG で 1 トランザクションか (使い捨て PG 専用)。

``KUEBIKO_PG_TEST_URL`` が無ければ skip。本番の DATABASE_URL は使わない・触らない。
例: docker run --rm -d --name kuebiko-pgtest2 -e POSTGRES_PASSWORD=x \\
      -p 127.0.0.1:55433:5432 postgres:16-alpine
    KUEBIKO_PG_TEST_URL=postgresql://postgres:x@127.0.0.1:55433/postgres \\
      uv run pytest tests/integration/test_db_atomic_pg.py
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from src.cti.severity_axes import EXTRA_FIELDS, STORED_FIELDS
from src.storage import db_backend
from src.storage.run_history import RunHistoryRepository

_URL = os.environ.get("KUEBIKO_PG_TEST_URL")

pytestmark = pytest.mark.skipif(not _URL, reason="KUEBIKO_PG_TEST_URL が未設定")

_DDL = (
    "CREATE TABLE IF NOT EXISTS atomic_t (v INTEGER)",
    "CREATE TABLE IF NOT EXISTS article_entities (article_id TEXT NOT NULL,"
    " entity_type TEXT NOT NULL, value TEXT NOT NULL, created_at TEXT NOT NULL,"
    " UNIQUE (article_id, entity_type, value))",
    "CREATE TABLE IF NOT EXISTS article_severity_axes (article_id TEXT PRIMARY KEY, "
    + ", ".join(f"{c} TEXT" for c in (*STORED_FIELDS, *EXTRA_FIELDS, "model", "created_at"))
    + ")",
    "CREATE TABLE IF NOT EXISTS article_importance_v2 (article_id TEXT PRIMARY KEY, x TEXT)",
)

_AXES = {
    "scope": "single_org",
    "impact": "data_exposure",
    "confirmation": "possible",
    "magnitude": "unknown",
    "exploitation": "not_applicable",
    "actor": "unknown",
    "target": "general_org",
}


class _BoomError(RuntimeError):
    pass


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> Iterator[RunHistoryRepository]:
    assert _URL is not None
    monkeypatch.setenv("DATABASE_URL", _URL)
    db_backend.close_pool()
    monkeypatch.setattr(db_backend, "_pg_schema_ensured", True)
    r = RunHistoryRepository()
    with r._connect() as conn:  # noqa: SLF001
        conn.execute("DROP TABLE IF EXISTS article_severity_axes")
        for ddl in _DDL:
            conn.execute(ddl)
        for t in ("atomic_t", "article_entities", "article_severity_axes", "article_importance_v2"):
            conn.execute(f"DELETE FROM {t}")  # noqa: S608
    yield r
    db_backend.close_pool()


def _count(repo: RunHistoryRepository, table: str) -> int:
    with repo._connect() as conn:  # noqa: SLF001
        return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])  # noqa: S608


def test_atomic_rolls_back_every_statement_on_pg(repo: RunHistoryRepository) -> None:
    conn: Any = db_backend.connect()
    try:
        with pytest.raises(_BoomError), db_backend.atomic(conn):
            conn.execute("INSERT INTO atomic_t VALUES (1)")
            conn.execute("INSERT INTO atomic_t VALUES (2)")
            raise _BoomError("mid-block")
    finally:
        conn.close()
    assert _count(repo, "atomic_t") == 0


def test_atomic_commits_on_pg(repo: RunHistoryRepository) -> None:
    conn: Any = db_backend.connect()
    try:
        with db_backend.atomic(conn):
            conn.execute("INSERT INTO atomic_t VALUES (1)")
    finally:
        conn.close()
    assert _count(repo, "atomic_t") == 1


def test_pir_rebuild_shape_never_shows_empty_to_readers(repo: RunHistoryRepository) -> None:
    """persist.py と同じ形 (DELETE + 再投入を repo.atomic で) で、読み手が 0 件を見ない。"""
    repo.add_article_entities("seed", [("pir", "p0")])
    seen_empty = threading.Event()
    stop = threading.Event()

    def reader() -> None:
        r = RunHistoryRepository()
        while not stop.is_set():
            with r._connect() as conn:  # noqa: SLF001
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM article_entities WHERE entity_type='pir'"
                ).fetchone()["c"]
            if n == 0:
                seen_empty.set()
                return

    t = threading.Thread(target=reader)
    t.start()
    try:
        for _ in range(5):
            with repo.atomic():
                with repo._connect() as conn:  # noqa: SLF001
                    conn.execute("DELETE FROM article_entities WHERE entity_type='pir'")
                for i in range(300):
                    repo.add_article_entities(f"a{i}", [("pir", "p1")])
    finally:
        stop.set()
        t.join(timeout=30)

    assert not seen_empty.is_set()
    assert _count(repo, "article_entities") == 300


def test_pir_rebuild_failure_keeps_old_links_on_pg(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.add_article_entities("old", [("pir", "p0")])
    real_add = repo.add_article_entities
    calls: list[str] = []

    def flaky(aid: str, ents: list[tuple[str, str]], **kw: Any) -> int:
        calls.append(aid)
        if len(calls) == 3:
            raise _BoomError("third insert")
        return real_add(aid, ents, **kw)

    with monkeypatch.context() as m:
        m.setattr(repo, "add_article_entities", flaky)
        with pytest.raises(_BoomError), repo.atomic():
            with repo._connect() as conn:  # noqa: SLF001
                conn.execute("DELETE FROM article_entities WHERE entity_type='pir'")
            for i in range(5):
                repo.add_article_entities(f"n{i}", [("pir", "p1")])

    assert repo.get_article_entities("old") == [("pir", "p0")]
    assert _count(repo, "article_entities") == 1


def test_severity_axes_replace_failure_keeps_old_rows_on_pg(repo: RunHistoryRepository) -> None:
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO article_severity_axes (article_id, scope, model, created_at)"
            " VALUES ('a1', 'single_org', 'old-model', 't')"
        )
        conn.execute("INSERT INTO article_importance_v2 (article_id, x) VALUES ('a1', 'k')")

    with pytest.raises(Exception, match="duplicate|unique|UniqueViolation"):
        repo.replace_severity_axes([("a1", _AXES), ("a1", _AXES)], "new-model")

    with repo._connect() as conn:  # noqa: SLF001
        row = conn.execute("SELECT model FROM article_severity_axes WHERE article_id='a1'")
        assert row.fetchone()["model"] == "old-model"
    assert _count(repo, "article_importance_v2") == 1


def test_real_rebuild_pir_entities_on_pg_never_shows_empty_to_readers(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本物の ``rebuild_pir_entities`` (atomic + advisory lock) で、読み手が 0 件を見ない。

    記事の読み込みと照合だけ差し替える (空 DB に全 schema が作れないため)。
    """
    from src.pir import persist
    from src.pir.models import Pir, PirConfig, StrongSignals

    cfg = PirConfig(
        priorities=[Pir(id="p1", title="T", strong_signals=StrongSignals(keywords=["x"]))]
    )
    rows = [{"article_id": f"a{i}"} for i in range(300)]
    monkeypatch.setattr(persist, "get_pir_config", lambda *a, **k: cfg)
    monkeypatch.setattr(persist, "_load_posted_rows", lambda **k: (rows, {}))
    monkeypatch.setattr(persist, "pir_match_signals", lambda *a, **k: True)
    repo.add_article_entities("seed", [("pir", "p0")])
    seen_empty = threading.Event()
    stop = threading.Event()

    def reader() -> None:
        r = RunHistoryRepository()
        while not stop.is_set():
            with r._connect() as conn:  # noqa: SLF001
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM article_entities WHERE entity_type='pir'"
                ).fetchone()["c"]
            if n == 0:
                seen_empty.set()
                return

    t = threading.Thread(target=reader)
    t.start()
    try:
        for _ in range(3):
            stats = persist.rebuild_pir_entities(repo=repo)
    finally:
        stop.set()
        t.join(timeout=30)

    assert stats["pir_links"] == 300
    assert not seen_empty.is_set()
    assert _count(repo, "article_entities") == 300


def test_other_thread_on_same_repo_instance_does_not_see_uncommitted_rows(
    repo: RunHistoryRepository,
) -> None:
    seen: dict[str, int] = {}
    inside = threading.Event()
    done = threading.Event()

    def other() -> None:
        inside.wait(10)
        with repo._connect() as conn:  # noqa: SLF001 — 同じ repo インスタンス、別スレッド
            seen["n"] = conn.execute("SELECT COUNT(*) AS c FROM atomic_t").fetchone()["c"]
        done.set()

    t = threading.Thread(target=other)
    t.start()
    with repo.atomic():
        with repo._connect() as conn:  # noqa: SLF001
            conn.execute("INSERT INTO atomic_t VALUES (1)")
        inside.set()
        assert done.wait(10)  # 他スレッドの読みが終わるまで未コミットのまま
    t.join(timeout=10)

    assert seen["n"] == 0
    assert _count(repo, "atomic_t") == 1


def test_concurrent_rebuild_writer_waits_instead_of_erroring(repo: RunHistoryRepository) -> None:
    """同じ advisory lock の 2 つ目のトランザクションはエラーでなく、1 つ目の COMMIT まで待つ。"""
    import time

    from src.pir.persist import PIR_REBUILD_LOCK_KEY

    holding = threading.Event()
    waited: dict[str, float] = {}
    errors: list[BaseException] = []

    def first() -> None:
        r = RunHistoryRepository()
        with r.atomic(), r._connect() as conn:  # noqa: SLF001
            conn.execute("SELECT pg_advisory_xact_lock(?)", (PIR_REBUILD_LOCK_KEY,))
            holding.set()
            time.sleep(1.0)

    def second() -> None:
        holding.wait(10)
        try:
            r = RunHistoryRepository()
            start = time.monotonic()
            with r.atomic(), r._connect() as conn:  # noqa: SLF001
                conn.execute("SELECT pg_advisory_xact_lock(?)", (PIR_REBUILD_LOCK_KEY,))
                waited["s"] = time.monotonic() - start
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)

    assert errors == []
    assert waited["s"] >= 0.5


def test_failed_lastval_inside_atomic_does_not_abort_the_transaction(
    repo: RunHistoryRepository,
) -> None:
    conn: Any = db_backend.connect()
    try:
        with db_backend.atomic(conn):
            cur = conn.execute("INSERT INTO atomic_t VALUES (1)")  # 列に sequence 無し
            assert cur.lastrowid is None  # LASTVAL は失敗するが SAVEPOINT で吸収
            conn.execute("INSERT INTO atomic_t VALUES (2)")
    finally:
        conn.close()
    assert _count(repo, "atomic_t") == 2
