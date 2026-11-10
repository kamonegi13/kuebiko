"""原子化した複数文書き込み (H1-H3, M1, M4-M6) が実 PG でロールバックされるか (使い捨て PG 専用)。

``KUEBIKO_PG_TEST_URL`` が無ければ skip。本番の DATABASE_URL は使わない・触らない。
例: docker run --rm -d --name kuebiko-pgtest-atomic -e POSTGRES_PASSWORD=x \\
      -p 127.0.0.1:55439:5432 postgres:16-alpine
    KUEBIKO_PG_TEST_URL=postgresql://postgres:x@127.0.0.1:55439/postgres \\
      uv run pytest tests/integration/test_atomic_write_holes_pg.py
単体 (SQLite) 版は tests/unit/test_atomic_write_holes.py。ここは同じ失敗注入を PG のトリガで行う
(PG はトランザクション内で文が失敗すると aborted になるため、SQLite と挙動が違う)。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from types import SimpleNamespace

import numpy as np
import pytest

from src.assessment.situation_store import SituationStore
from src.cti.actor_observed_history import ActorMonthProfile
from src.eventnews import runner
from src.storage import db_backend
from src.storage.run_history import RunHistoryRepository
from tests.integration.test_db_atomic_pg import _URL
from tests.unit import test_atomic_write_holes as u

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _URL, reason="KUEBIKO_PG_TEST_URL が未設定"),
]

_TABLES = (
    "actor_observed_profile",
    "event_item_versions",
    "event_item_members",
    "event_items",
    "article_entities",
    "situation_relations",
    "situation_evidence",
    "situations",
    "articles",
    "runs",
)


def _sql(repo: RunHistoryRepository, *stmts: str) -> None:
    with repo._connect() as conn:  # noqa: SLF001
        for s in stmts:
            conn.execute(s)


def _trigger(
    repo: RunHistoryRepository, table: str, event: str, when: str, name: str = "boom"
) -> None:
    """``when`` に合う行の ``event`` で例外を投げるトリガ (BEFORE、行単位)。"""
    _sql(
        repo,
        f"CREATE OR REPLACE FUNCTION {name}_fn() RETURNS trigger AS $$ BEGIN "  # noqa: S608
        "RAISE EXCEPTION 'boom'; END $$ LANGUAGE plpgsql",
        f"DROP TRIGGER IF EXISTS {name} ON {table}",
        f"CREATE TRIGGER {name} BEFORE {event} ON {table} FOR EACH ROW "
        f"WHEN ({when}) EXECUTE FUNCTION {name}_fn()",
    )


def _drop_trigger(repo: RunHistoryRepository, table: str, name: str = "boom") -> None:
    _sql(repo, f"DROP TRIGGER IF EXISTS {name} ON {table}")


def _apply_schema_to_fresh_pg() -> None:
    """空の PG へ schema を入れる。

    pg_schema.py は ALTER (event_item_versions 等) が対応する CREATE より前にある箇所があり、
    空の DB へ一括で流すと UndefinedTable で落ちる (本番は表が既にあるので出ない)。
    ここでは文を 1 つずつ流し、失敗した文は後ろへ回して収束するまで繰り返す。
    """
    from src.storage.pg_schema import get_schema_sql

    pending = [
        st.strip()
        for st in "\n".join(
            ln for ln in get_schema_sql().splitlines() if not ln.lstrip().startswith("--")
        ).split(";")
        if st.strip()
    ]
    for _ in range(6):
        failed: list[str] = []
        for st in pending:
            try:
                with db_backend.transaction() as conn:
                    conn.execute(st)
            except Exception:  # noqa: BLE001
                failed.append(st)
        if not failed:
            return
        pending = failed
    raise AssertionError(f"schema が収束しない: {len(pending)} 文")


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> Iterator[RunHistoryRepository]:
    assert _URL is not None
    monkeypatch.setenv("DATABASE_URL", _URL)
    db_backend.close_pool()
    monkeypatch.setattr(db_backend, "_pg_schema_ensured", False)
    _apply_schema_to_fresh_pg()
    r = RunHistoryRepository()
    with r._connect() as conn:  # noqa: SLF001
        for t in _TABLES:
            conn.execute(f"TRUNCATE {t} CASCADE")  # noqa: S608
    yield r
    for t, n in (
        ("actor_observed_profile", "boom"),
        ("article_entities", "boom"),
        ("event_items", "boom"),
        ("articles", "boom"),
        ("situations", "boom"),
    ):
        _drop_trigger(r, t, n)
    db_backend.close_pool()


def test_h1_replace_month_profiles_rolls_back_on_pg(repo: RunHistoryRepository) -> None:
    repo.replace_actor_month_profiles("2026-10", [ActorMonthProfile(actor_id="a", month="2026-10")])
    _trigger(repo, "actor_observed_profile", "INSERT", "NEW.actor_id = 'bad'")
    with pytest.raises(Exception, match="boom"):
        repo.replace_actor_month_profiles(
            "2026-10",
            [
                ActorMonthProfile(actor_id="c", month="2026-10"),
                ActorMonthProfile(actor_id="bad", month="2026-10"),
            ],
        )
    assert u._scalar(repo, "SELECT string_agg(actor_id, ',') FROM actor_observed_profile") == "a"


async def test_h2_seed_item_and_member_all_or_nothing_on_pg(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**_: object) -> None:
        raise RuntimeError("member write failed")

    monkeypatch.setattr(repo, "add_event_member", boom)
    with pytest.raises(RuntimeError):
        await runner.process_candidates(
            repo,
            [u._member("a1")],
            {"a1": np.ones(4, dtype=np.float32)},
            "live",
            None,
            generate=False,
        )
    assert u._scalar(repo, "SELECT COUNT(*) FROM event_items") == 0


def test_h3_reenrich_rolls_back_entity_delete_on_pg(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.pipeline import reprocess

    u._add_article(repo, "a1")
    repo.add_article_entities("a1", [("actor", "apt_x"), ("cve", "CVE-2026-1")])
    row = repo.get_article("a1")
    assert row is not None

    def boom(**_: object) -> None:
        raise RuntimeError("persist failed")

    monkeypatch.setattr(reprocess, "_persist_article_entities", boom)
    monkeypatch.setattr(reprocess, "_load_registry", lambda: SimpleNamespace())
    msg = SimpleNamespace(metadata={}, title="", summary="")
    with pytest.raises(RuntimeError):
        reprocess.reenrich_stored_article(repo, "a1", row, msg)  # type: ignore[arg-type]
    assert u._scalar(repo, "SELECT COUNT(*) FROM article_entities WHERE article_id='a1'") == 2


def test_m1_promote_provisional_rolls_back_on_pg(repo: RunHistoryRepository) -> None:
    u._add_article(repo, "a1")
    repo.add_article_entities("a1", [("actor_provisional", "newgrp")])
    _trigger(repo, "article_entities", "DELETE", "OLD.entity_type = 'actor_provisional'")
    with pytest.raises(Exception, match="boom"):
        repo.promote_provisional_actor("newgrp", "new_group")
    assert u._scalar(repo, "SELECT COUNT(*) FROM article_entities WHERE entity_type='actor'") == 0
    assert (
        u._scalar(
            repo, "SELECT COUNT(*) FROM article_entities WHERE entity_type='actor_provisional'"
        )
        == 1
    )


def _store(repo: RunHistoryRepository) -> SituationStore:
    return SituationStore(db_path=repo.db_path)


def test_m4_merge_situation_rolls_back_on_pg(repo: RunHistoryRepository) -> None:
    store = _store(repo)
    keep, dup = u._open(store, "残る"), u._open(store, "消える")
    store.record_assignment(situation_id=dup, article_id="a1", added_at=u._ISO, assigned_by="seed")
    _trigger(repo, "situations", "UPDATE", "NEW.merged_into IS NOT NULL")
    with pytest.raises(Exception, match="boom"):
        store.merge_situation(dup_id=dup, into_id=keep, basis="b", now_iso=u._ISO)
    sql = "SELECT COUNT(*) FROM situation_evidence WHERE situation_id=?"
    assert u._scalar(repo, sql, (dup,)) == 1
    assert u._scalar(repo, sql, (keep,)) == 0


def _race_merges(
    store: SituationStore, pairs: list[tuple[str, str]]
) -> tuple[list[BaseException], bool]:
    """(dup, into) の組を別スレッド・別接続で同時に merge する。(例外の一覧, ハングしたか)。"""
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(pairs))

    def run(dup: str, into: str) -> None:
        try:
            barrier.wait(timeout=10)
            store.merge_situation(dup_id=dup, into_id=into, basis="c", now_iso=u._ISO)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=p) for p in pairs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return errors, any(t.is_alive() for t in threads)


def _open_with_evidence(store: SituationStore, title: str, prefix: str) -> str:
    sid = u._open(store, title)
    for i in range(20):
        store.record_assignment(
            situation_id=sid, article_id=f"{prefix}{i}", added_at=u._ISO, assigned_by="seed"
        )
    return sid


def test_m4_concurrent_merges_same_direction_no_deadlock(repo: RunHistoryRepository) -> None:
    """同じ組 (a→b) を 2 接続で同時に merge しても、デッドロックやハングは無い。"""
    store = _store(repo)
    a = _open_with_evidence(store, "情勢A", "a")
    b = _open_with_evidence(store, "情勢B", "b")
    errors, hung = _race_merges(store, [(a, b), (a, b)])
    print("same-direction errors:", [repr(e) for e in errors])  # noqa: T201
    assert not hung
    assert not errors


def test_m4_concurrent_merges_opposite_direction_no_deadlock(repo: RunHistoryRepository) -> None:
    """逆向き (c→d と d→c) を同時に merge しても、デッドロックやハングは無い。"""
    store = _store(repo)
    c = _open_with_evidence(store, "情勢C", "c")
    d = _open_with_evidence(store, "情勢D", "d")
    errors, hung = _race_merges(store, [(c, d), (d, c)])
    print("opposite-direction errors:", [repr(e) for e in errors])  # noqa: T201
    assert not hung
    assert not errors


async def test_m5_version_and_current_version_bump_atomic_on_pg(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    item, llm = u.prepare_m5(repo, monkeypatch)
    _trigger(repo, "event_items", "UPDATE", "NEW.current_version = 1")
    with pytest.raises(Exception, match="boom"):
        await runner._generate_version(  # noqa: SLF001
            repo, item, llm, u._NOW, "[]", bump_current_version_at=u._NOW
        )
    assert u._scalar(repo, "SELECT COUNT(*) FROM event_item_versions") == 0


def test_m6_promote_thin_body_rolls_back_on_pg(repo: RunHistoryRepository) -> None:
    reason = repo.THIN_BODY_FAILURE_REASON
    u._add_article(repo, "a", status="extract_failed", reason=reason)
    u._add_article(repo, "a", status="extract_failed", reason=reason)
    newest = int(u._scalar(repo, "SELECT MAX(id) FROM articles WHERE article_id='a'"))
    _trigger(repo, "articles", "UPDATE", "NEW.failure_reason LIKE 'superseded%'")
    with pytest.raises(Exception, match="boom"):
        repo.promote_recovered_thin_body(newest, importance="low", category=None)
    assert u._scalar(repo, "SELECT status FROM articles WHERE id=?", (newest,)) == "extract_failed"
