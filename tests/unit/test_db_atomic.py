"""``db_backend.atomic`` / ``repo.atomic`` と、それを使う 4 つの複数文の書き込みの原子性。

SQLite は ``isolation_level=None`` (autocommit) なので、明示の BEGIN が無いと途中の失敗で
前半の文が確定する。各テストは「途中で例外 → 何も書かれていない / 旧い状態が残る」を固定する。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.eventnews.merge import MergeGroup
from src.storage import db_backend
from src.storage.repo_merge_decisions import MergeDecisionRow
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.services import eventnews_merge_job as job
from src.ui.services.eventnews_merge_undo import undo_merge
from tests.unit.test_eventnews_merge_job import _inputs
from tests.unit.test_eventnews_merge_undo import _fake_loader, _members, _merge

_NOW = datetime(2026, 10, 22, 6, 0, 0, tzinfo=UTC)

_AXES = {
    "scope": "single_org",
    "impact": "data_exposure",
    "confirmation": "possible",
    "magnitude": "unknown",
    "exploitation": "not_applicable",
    "actor": "unknown",
    "target": "general_org",
}


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "atomic.db")


class _BoomError(RuntimeError):
    pass


def _count(path: Path) -> int:
    check = sqlite3.connect(path)
    try:
        return int(check.execute("SELECT COUNT(*) FROM t").fetchone()[0])
    finally:
        check.close()


class TestAtomicContext:
    def _conn(self, path: Path) -> Any:
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("CREATE TABLE t (v INTEGER)")
        return conn

    def test_commits_on_success(self, tmp_path: Path) -> None:
        path = tmp_path / "t.db"
        conn = self._conn(path)
        with db_backend.atomic(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            conn.execute("INSERT INTO t VALUES (2)")
        assert _count(path) == 2

    def test_rolls_back_all_statements_and_reraises(self, tmp_path: Path) -> None:
        path = tmp_path / "t.db"
        conn = self._conn(path)
        with pytest.raises(_BoomError), db_backend.atomic(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            raise _BoomError("mid-block")
        assert _count(path) == 0

    def test_original_exception_wins_when_rollback_fails(self) -> None:
        class _Conn:
            def execute(self, sql: str, *a: object) -> None:
                if sql == "ROLLBACK":
                    raise sqlite3.OperationalError("rollback failed")

        with pytest.raises(_BoomError), db_backend.atomic(_Conn()):
            raise _BoomError("original")

    def test_transaction_wrapper_is_real(self, tmp_path: Path, monkeypatch: Any) -> None:
        path = tmp_path / "w.db"
        self._conn(path).close()
        monkeypatch.setattr(
            db_backend, "connect", lambda: sqlite3.connect(path, isolation_level=None)
        )
        with pytest.raises(_BoomError), db_backend.transaction() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise _BoomError("x")
        assert _count(path) == 0


class TestPirRebuild:
    def test_failure_mid_rebuild_keeps_old_links(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.pir import persist
        from src.pir.models import Pir, PirConfig, StrongSignals

        run_id = repo.start_run(RunRecord(started_at=_NOW, pipeline="t", dry_run=True))
        for aid, title in (("m1", "Lazarus one"), ("m2", "Lazarus two")):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=aid,
                    title=title,
                    url=f"https://x.example/{aid}",
                    category="apt",
                    status="posted",
                    importance="high",
                    summary=title,
                    published_at=datetime.now(UTC),
                )
            )
        repo.add_article_entities("old", [("pir", "pir_old")])
        cfg = PirConfig(
            priorities=[
                Pir(id="pir_new", title="T", strong_signals=StrongSignals(keywords=["lazarus"]))
            ]
        )
        monkeypatch.setattr(persist, "get_pir_config", lambda *a, **k: cfg)
        real_add = repo.add_article_entities
        calls: list[str] = []

        def flaky(aid: str, ents: list[tuple[str, str]], **kw: Any) -> int:
            calls.append(aid)
            if len(calls) == 2:
                raise _BoomError("second insert")
            return real_add(aid, ents, **kw)

        monkeypatch.setattr(repo, "add_article_entities", flaky)

        with pytest.raises(_BoomError):
            persist.rebuild_pir_entities(repo=repo)

        monkeypatch.undo()
        assert repo.get_article_entities("old") == [("pir", "pir_old")]
        assert repo.get_article_entities("m1") == []


class TestSeverityAxesReplace:
    def test_failure_in_a_chunk_keeps_old_axes_and_importance(
        self, repo: RunHistoryRepository
    ) -> None:
        repo.set_severity_axes("a1", _AXES, "old-model")
        with repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO article_importance_v2 (article_id, severity, severity_basis,"
                " strategic_weight, jp, nations, sir_ids, relevant, rule_version, created_at)"
                " VALUES ('a1', 'S2', 'x', NULL, 'none', '[]', '[]', 0, 'v', '2026-10-27')"
            )

        # 同じ記事を 2 回書く = 2 つ目の INSERT が主キー違反 (DELETE の後・INSERT の途中で失敗)
        with pytest.raises(sqlite3.IntegrityError):
            repo.replace_severity_axes([("a1", _AXES), ("a1", _AXES)], "new-model")

        assert repo.get_severity_axes(["a1"])["a1"]["model"] == "old-model"
        with repo._connect() as conn:  # noqa: SLF001
            assert conn.execute("SELECT COUNT(*) FROM article_importance_v2").fetchone()[0] == 1


def _seed_items(repo: RunHistoryRepository) -> None:
    for iid in ("i1", "i2"):
        repo.create_event_item(
            item_id=iid,
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="low",
            when=_NOW,
        )
        repo.update_event_item(iid, {"current_version": 1})
    repo.add_event_member(
        item_id="i1", article_id="a1", joined_at=_NOW, contributed_new_facts=0, join_signal="seed"
    )


class TestApplyMergeGroupAtomically:
    def _plan(self) -> job.MergePlan:
        return job.MergePlan(groups=(MergeGroup(target="i1", absorbed=("i2",)),), inputs=_inputs())

    def _row(self) -> MergeDecisionRow:
        return MergeDecisionRow(
            target_id="i1",
            absorbed_id="i2",
            run_kind="hourly",
            edges_json="[]",
            edge_count=1,
            rescued=False,
            flags_json="{}",
        )

    def test_decision_record_failure_rolls_the_merge_back(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_items(repo)
        monkeypatch.setattr(job, "decision_rows", lambda plan, *, run_kind: [self._row()])

        def boom(*_a: object, **_k: object) -> int:
            raise _BoomError("record failed")

        monkeypatch.setattr(repo, "record_merge_decisions", boom)
        plan = self._plan()

        with pytest.raises(_BoomError):
            job.apply_group_atomically(repo, plan, plan.groups[0], _NOW, run_kind="hourly")

        i1, i2 = repo.get_event_item("i1"), repo.get_event_item("i2")
        assert i1 is not None and i2 is not None
        assert i1.state.current_version == 1  # 本文は消えていない
        assert i2.merged_into is None
        assert _members(repo, "i1") == {"a1"}

    def test_success_writes_merge_and_decision_together(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_items(repo)
        monkeypatch.setattr(job, "decision_rows", lambda plan, *, run_kind: [self._row()])
        plan = self._plan()

        absorbed, recorded = job.apply_group_atomically(
            repo, plan, plan.groups[0], _NOW, run_kind="hourly"
        )

        assert (absorbed, recorded) == (1, 1)
        i2 = repo.get_event_item("i2")
        assert i2 is not None and i2.merged_into == "i1"
        assert len(repo.list_merge_decisions(active_only=True)) == 1


class TestUndoMergeAtomically:
    def test_failure_late_in_undo_leaves_everything_merged(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _merge(repo)

        def boom(*_a: object, **_k: object) -> None:
            raise _BoomError("mark failed")

        monkeypatch.setattr(repo, "mark_merge_decisions_undone", boom)

        with pytest.raises(_BoomError):
            undo_merge(repo, "ev-b", reason="x", now=_NOW, load_members=_fake_loader)

        b = repo.get_event_item("ev-b")
        a = repo.get_event_item("ev-a")
        assert b is not None and a is not None
        assert b.merged_into == "ev-a"  # 外れていない
        assert _members(repo, "ev-a") == {"x1", "y1", "y2"}  # 行も消えていない
        assert a.state.current_version == 3  # 作り直されていない

        monkeypatch.undo()  # 再実行で完了する (収束)
        res = undo_merge(repo, "ev-b", reason="x", now=_NOW, load_members=_fake_loader)
        assert res.status == "restored"


class TestGroupFailureIsolation:
    async def test_one_failing_group_does_not_stop_others_or_regeneration(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for iid in ("i1", "i2", "i3", "i4"):
            repo.create_event_item(
                item_id=iid,
                origin="live",
                first_reported_at=_NOW,
                last_reported_at=_NOW,
                importance="low",
                when=_NOW,
            )
            repo.update_event_item(iid, {"current_version": 1})
        bad = MergeGroup(target="i1", absorbed=("i2",))
        good = MergeGroup(target="i3", absorbed=("i4",))
        plan = job.MergePlan(groups=(bad, good), inputs=_inputs())

        async def fake_prepare(*_a: object, **_k: object) -> tuple[None, int]:
            return None, 0

        regen_calls: list[int] = []

        async def fake_regen(*_a: object, **_k: object) -> tuple[Any, int]:
            regen_calls.append(1)
            stats = type("S", (), {"generated": 0, "failed": 0, "skipped": 0, "attempted": 0})()
            return stats, 0

        def fake_apply(repo_: Any, plan_: Any, group: Any, now: Any, *, run_kind: str) -> Any:
            if group is bad:
                with repo_.atomic():
                    repo_.update_event_item("i2", {"merged_into": "i1"})
                    raise _BoomError("group failed")
            with repo_.atomic():
                repo_.update_event_item("i4", {"merged_into": "i3"})
            return 1, 0

        monkeypatch.setattr(job, "_prepare_inputs", fake_prepare)
        monkeypatch.setattr(job, "plan_for", lambda *_a, **_k: plan)
        monkeypatch.setattr(job, "regenerate_pending", fake_regen)
        monkeypatch.setattr(job, "apply_group_atomically", fake_apply)

        outcome = await job.merge_and_regenerate(repo, apply=True, regen_limit=1)

        assert outcome.applied_groups == 1
        assert outcome.extra["merge_groups_failed"] == 1
        assert regen_calls == [1]
        i2, i4 = repo.get_event_item("i2"), repo.get_event_item("i4")
        assert i2 is not None and i4 is not None
        assert i2.merged_into is None  # 失敗した群は触られていない
        assert i4.merged_into == "i3"


class TestAtomicBoundToTask:
    async def test_other_task_on_same_thread_gets_its_own_connection(
        self, repo: RunHistoryRepository
    ) -> None:
        import asyncio

        seen: dict[str, Any] = {}
        with repo.atomic(), repo._connect() as owner:  # noqa: SLF001
            owner.execute("CREATE TABLE IF NOT EXISTS tt (v INTEGER)")

            async def other() -> None:
                with repo._connect() as conn:  # noqa: SLF001
                    seen["same"] = conn is owner

            await asyncio.create_task(other())

        assert seen["same"] is False
