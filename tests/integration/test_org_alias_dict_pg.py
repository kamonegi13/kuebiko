"""org_alias_dict の置き換えが実 PG で 1 トランザクションか (使い捨て PG 専用)。

``KUEBIKO_PG_TEST_URL`` が無ければ skip。本番の DATABASE_URL は使わない・触らない。
例: docker run --rm -d --name kuebiko-pgtest -e POSTGRES_PASSWORD=x \\
      -p 127.0.0.1:55432:5432 postgres:16-alpine
    KUEBIKO_PG_TEST_URL=postgresql://postgres:x@127.0.0.1:55432/postgres uv run pytest <this>
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator

import pytest

from src.storage import db_backend
from src.storage.run_history import RunHistoryRepository

_URL = os.environ.get("KUEBIKO_PG_TEST_URL")

_DDL = (
    "CREATE TABLE IF NOT EXISTS org_alias_dict (alias_key TEXT NOT NULL PRIMARY KEY,"
    " canonical_key TEXT NOT NULL, source TEXT NOT NULL, updated_at TEXT NOT NULL)"
)

pytestmark = pytest.mark.skipif(not _URL, reason="KUEBIKO_PG_TEST_URL が未設定")


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> Iterator[RunHistoryRepository]:
    assert _URL is not None
    monkeypatch.setenv("DATABASE_URL", _URL)
    db_backend.close_pool()
    # 全 schema は空 DB だと index 順で失敗しうる (既知) ので、対象の表だけ作る
    monkeypatch.setattr(db_backend, "_pg_schema_ensured", True)
    r = RunHistoryRepository()
    with r._connect() as conn:
        conn.execute(_DDL)
        conn.execute("DELETE FROM org_alias_dict")
    yield r
    with r._connect() as conn:
        conn.execute("DELETE FROM org_alias_dict")
    db_backend.close_pool()


class _FailingItems(dict[str, str]):
    def items(self):  # type: ignore[no-untyped-def]
        def gen() -> Iterator[tuple[str, str]]:
            yield ("n1", "n1")
            raise RuntimeError("boom mid-insert")

        return gen()


def test_failure_mid_insert_keeps_old_rows(repo: RunHistoryRepository) -> None:
    repo.replace_org_alias_dict({"old1": "old1", "old2": "old1"}, source="pairs")

    with pytest.raises(RuntimeError, match="boom mid-insert"):
        repo.replace_org_alias_dict(_FailingItems(n1="n1"), source="pairs")

    assert repo.load_org_alias_dict() == {"old1": "old1", "old2": "old1"}


def test_readers_never_see_an_empty_table_during_refresh(repo: RunHistoryRepository) -> None:
    repo.replace_org_alias_dict({"seed": "seed"}, source="pairs")
    big = {f"k{i:05d}": "k00000" for i in range(3000)}
    seen_empty = threading.Event()
    stop = threading.Event()

    def reader() -> None:
        reader_repo = RunHistoryRepository()
        while not stop.is_set():
            if not reader_repo.load_org_alias_dict():
                seen_empty.set()
                return

    t = threading.Thread(target=reader)
    t.start()
    try:
        for _ in range(3):
            repo.replace_org_alias_dict(big, source="pairs")
    finally:
        stop.set()
        t.join(timeout=30)

    assert not seen_empty.is_set()
    assert len(repo.load_org_alias_dict()) == len(big)


def test_manual_row_is_not_taken_over_on_pg(repo: RunHistoryRepository) -> None:
    repo.replace_org_alias_dict({"aa": "manual-canon"}, source="manual")

    repo.replace_org_alias_dict({"aa": "aa", "bb": "aa"}, source="pairs")

    assert repo.load_org_alias_dict() == {"aa": "manual-canon", "bb": "aa"}
