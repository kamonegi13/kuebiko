"""複数文の書き込みの原子性 (autocommit の PG/SQLite で途中失敗が前半を確定させる穴の塞ぎ)。

各テストは「途中で例外 → ロールバックされ、前の状態が残る」を固定する。DB 側の失敗は
SQLite のトリガ (RAISE(ABORT)) で、Python 側の失敗は差し替えで注入する。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from src.assessment.situation_store import SituationStore
from src.cti.actor_observed_history import ActorMonthProfile
from src.eventnews import generator as gen
from src.eventnews import identifier_gate, quantities, runner
from src.eventnews.models import EventNewsDraft, GateResult, ItemState, MemberArticle
from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository

_NOW = datetime(2026, 11, 4, 0, 0, 0, tzinfo=UTC)
_ISO = _NOW.isoformat()


def _repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "holes.db")


def _trigger(repo: RunHistoryRepository, sql: str) -> None:
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(sql)


def _scalar(repo: RunHistoryRepository, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with repo._connect() as conn:  # noqa: SLF001
        return conn.execute(sql, params).fetchone()[0]


def _add_article(
    repo: RunHistoryRepository, aid: str, *, status: str = "posted", reason: str | None = None
) -> None:
    rid = repo.start_run(RunRecord(started_at=_NOW, pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title="t",
            url=f"https://ex.example/{aid}",
            status=status,  # type: ignore[arg-type]
            failure_reason=reason,
        )
    )


# ---- H1 ----
def test_h1_replace_month_profiles_rolls_back_on_insert_failure(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.replace_actor_month_profiles("2026-10", [ActorMonthProfile(actor_id="a", month="2026-10")])
    _trigger(
        repo,
        "CREATE TRIGGER boom BEFORE INSERT ON actor_observed_profile "
        "WHEN NEW.actor_id='bad' BEGIN SELECT RAISE(ABORT, 'boom'); END",
    )
    with pytest.raises(sqlite3.DatabaseError):
        repo.replace_actor_month_profiles(
            "2026-10",
            [
                ActorMonthProfile(actor_id="c", month="2026-10"),
                ActorMonthProfile(actor_id="bad", month="2026-10"),
            ],
        )
    actors = _scalar(repo, "SELECT group_concat(actor_id) FROM actor_observed_profile")
    assert actors == "a"


# ---- H2 ----
def _member(aid: str) -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=f"t-{aid}",
        url=f"https://e.example/{aid}",
        feed_title="F",
        feed_url="https://f.example",
        host="e.example",
        importance="high",
        category="apt",
        status="posted",
        anchor_ts=_NOW,
        summary="s",
        body="",
        entities=frozenset({("cve", "cve-2026-1111")}),
    )


async def test_h2_seed_item_and_member_are_all_or_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)

    def boom(**_: object) -> None:
        raise RuntimeError("member write failed")

    monkeypatch.setattr(repo, "add_event_member", boom)
    with pytest.raises(RuntimeError):
        await runner.process_candidates(
            repo,
            [_member("a1")],
            {"a1": np.ones(4, dtype=np.float32)},
            "live",
            None,
            generate=False,
        )
    assert _scalar(repo, "SELECT COUNT(*) FROM event_items") == 0


# ---- H3 ----
def test_h3_reenrich_rolls_back_entity_delete_when_persist_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.pipeline import reprocess

    repo = _repo(tmp_path)
    _add_article(repo, "a1")
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
    assert _scalar(repo, "SELECT COUNT(*) FROM article_entities WHERE article_id='a1'") == 2


def test_h3_persist_entities_reraises_only_when_asked() -> None:
    from src.pipeline.persistence import _persist_article_entities

    class _Repo:
        def add_article_entities(self, *_: object) -> int:
            raise RuntimeError("db down")

        def record_alias_usage(self, *_: object) -> None:
            return None

    msg = SimpleNamespace(
        mitre_techniques=["T1059"], iocs=[], metadata={}, title="", summary="", body=""
    )
    kw: dict[str, Any] = {"dedup_repo": _Repo(), "article_id": "a1", "msg": msg}
    _persist_article_entities(**kw)  # 既定は握って警告のみ
    with pytest.raises(RuntimeError):
        _persist_article_entities(**kw, raise_on_error=True)


# ---- M1 ----
def test_m1_promote_provisional_rolls_back_when_delete_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_article(repo, "a1")
    repo.add_article_entities("a1", [("actor_provisional", "newgrp")])
    _trigger(
        repo,
        "CREATE TRIGGER boom BEFORE DELETE ON article_entities "
        "WHEN OLD.entity_type='actor_provisional' BEGIN SELECT RAISE(ABORT, 'boom'); END",
    )
    with pytest.raises(sqlite3.DatabaseError):
        repo.promote_provisional_actor("newgrp", "new_group")
    assert _scalar(repo, "SELECT COUNT(*) FROM article_entities WHERE entity_type='actor'") == 0


# ---- M4 ----
def _store(tmp_path: Path) -> SituationStore:
    db = tmp_path / "sit.db"
    RunHistoryRepository(db_path=db)
    return SituationStore(db_path=db)


def _open(store: SituationStore, title: str) -> str:
    return store.open_situation(
        title=title,
        domain="cyber",
        anchors=frozenset({"actor:x"}),
        pir_ids=(),
        now_iso="2026-09-01T00:00:00+00:00",
    ).situation_id


def test_m4_merge_situation_rolls_back_when_tombstone_fails(tmp_path: Path) -> None:
    store = _store(tmp_path)
    keep, dup = _open(store, "残る"), _open(store, "消える")
    store.record_assignment(situation_id=dup, article_id="a1", added_at=_ISO, assigned_by="seed")
    _trigger(
        store._repo,  # noqa: SLF001
        "CREATE TRIGGER boom BEFORE UPDATE ON situations "
        "WHEN NEW.merged_into IS NOT NULL BEGIN SELECT RAISE(ABORT, 'boom'); END",
    )
    with pytest.raises(sqlite3.DatabaseError):
        store.merge_situation(dup_id=dup, into_id=keep, basis="b", now_iso=_ISO)
    count = _scalar(
        store._repo,  # noqa: SLF001
        "SELECT COUNT(*) FROM situation_evidence WHERE situation_id=?",
        (dup,),
    )
    assert count == 1


# ---- M5 ----
def prepare_m5(repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """M5 の準備 (生成まわりの差し替えと item の作成)。(item, llm) を返す。PG 版も共用。"""
    repo.create_event_item(
        item_id="i1",
        origin="live",
        first_reported_at=_NOW,
        last_reported_at=_NOW,
        importance="high",
        when=_NOW,
    )
    item = runner._LiveItem(  # noqa: SLF001
        snapshot=ItemState(
            item_id="i1",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            status="new",
            importance="high",
            current_version=0,
            member_ids=("a1", "a2"),
        ),
        members=[_member("a1"), _member("a2")],
    )
    draft = EventNewsDraft(headline="h", bluf="b")

    async def fake_draft(*_a: object, **_k: object) -> EventNewsDraft:
        return draft

    monkeypatch.setattr(gen, "select_members", lambda m: (list(m), 0))
    monkeypatch.setattr(runner, "_should_generate", lambda *_a, **_k: True)
    monkeypatch.setattr(runner, "_allowed_identifiers_text", lambda _s: "")
    monkeypatch.setattr(gen, "build_prompt", lambda *_a, **_k: "p")
    monkeypatch.setattr(gen, "generate_draft", fake_draft)
    monkeypatch.setattr(
        identifier_gate,
        "verify_draft",
        lambda d, _s: GateResult(
            draft=d, dropped_lines=0, repaired_ids=0, substituted_ids=0, verified=True
        ),
    )
    monkeypatch.setattr(runner, "_rewrite_hints", lambda *_a, **_k: [])
    monkeypatch.setattr(quantities, "supporting_texts", lambda _s: [])
    monkeypatch.setattr(identifier_gate, "build_member_catalog", lambda _s: {})
    llm: Any = SimpleNamespace(model="m")
    return item, llm


async def test_m5_version_and_current_version_bump_are_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    item, llm = prepare_m5(repo, monkeypatch)
    _trigger(
        repo,
        "CREATE TRIGGER boom BEFORE UPDATE ON event_items "
        "WHEN NEW.current_version=1 BEGIN SELECT RAISE(ABORT, 'boom'); END",
    )
    with pytest.raises(sqlite3.DatabaseError):
        await runner._generate_version(  # noqa: SLF001
            repo, item, llm, _NOW, "[]", bump_current_version_at=_NOW
        )
    assert _scalar(repo, "SELECT COUNT(*) FROM event_item_versions") == 0


# ---- M6 ----
def test_m6_promote_thin_body_rolls_back_when_supersede_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    reason = repo.THIN_BODY_FAILURE_REASON
    _add_article(repo, "a", status="extract_failed", reason=reason)
    _add_article(repo, "a", status="extract_failed", reason=reason)
    newest = int(_scalar(repo, "SELECT MAX(id) FROM articles WHERE article_id='a'"))
    _trigger(
        repo,
        "CREATE TRIGGER boom BEFORE UPDATE ON articles "
        "WHEN NEW.failure_reason LIKE 'superseded%' BEGIN SELECT RAISE(ABORT, 'boom'); END",
    )
    with pytest.raises(sqlite3.DatabaseError):
        repo.promote_recovered_thin_body(newest, importance="low", category=None)
    status = _scalar(repo, "SELECT status FROM articles WHERE id=?", (newest,))
    assert status == "extract_failed"
