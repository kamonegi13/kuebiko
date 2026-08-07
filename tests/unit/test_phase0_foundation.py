"""Phase 0 (基盤) の検証 — F2 actor 永続化 + F3 orphan cleanup。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.main import _persist_article_entities
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.tools.discord_publisher import BriefingMessage


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "p0.db")


def _entities(repo: RunHistoryRepository, article_id: str) -> set[tuple[str, str]]:
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT entity_type, value FROM article_entities WHERE article_id=?",
            (article_id,),
        ).fetchall()
    return {(str(r["entity_type"]), str(r["value"])) for r in rows}


@pytest.mark.unit
def test_inline_pir_tagging_at_ingest(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    # L1a (2026-07-05): PIR タグを取込時 inline で付与する (旧: 日次バッチのみ)。
    import src.pir.integration as integ
    from src.pir.models import Pir, StrongSignals

    pir = Pir(id="pir_test", title="t", strong_signals=StrongSignals(actors=["Lazarus"]))

    class _Cfg:
        priorities = [pir]

    monkeypatch.setattr(integ, "get_pir_config", lambda force_reload=False: _Cfg())
    msg = BriefingMessage(
        title="Lazarus による攻撃",
        importance="high",
        category="apt",
        summary="s",
        metadata={"detected_actor_ids": ["lazarus"]},
    )
    _persist_article_entities(dedup_repo=repo, article_id="p1", msg=msg)
    assert ("pir", "pir_test") in _entities(repo, "p1")


# ---------- F2: Adversary entity 永続化 (礎石) ----------


@pytest.mark.unit
def test_actor_entity_persisted_from_detected_ids(repo: RunHistoryRepository) -> None:
    # Arrange: detected_actor_ids を持つ briefing
    msg = BriefingMessage(
        title="APT41 と Lazarus の共同オペレーション",
        importance="high",
        category="apt",
        summary="s",
        metadata={"detected_actor_ids": ["apt41", "lazarus"]},
    )
    # Act
    _persist_article_entities(dedup_repo=repo, article_id="a1", msg=msg)
    # Assert: actor entity が保存される
    ents = _entities(repo, "a1")
    assert ("actor", "apt41") in ents
    assert ("actor", "lazarus") in ents


@pytest.mark.unit
def test_actor_entity_dedup_and_empty(repo: RunHistoryRepository) -> None:
    # 重複 actor は 1 行に集約、detected なしなら actor 行は作られない
    msg_dup = BriefingMessage(
        title="t",
        importance="medium",
        category="apt",
        summary="s",
        metadata={"detected_actor_ids": ["lazarus", "lazarus"]},
    )
    _persist_article_entities(dedup_repo=repo, article_id="dup", msg=msg_dup)
    # actor 行のみを検証 (inline PIR タグ 2026-07-05 で pir 行も付きうるため type scope)
    assert {e for e in _entities(repo, "dup") if e[0] == "actor"} == {("actor", "lazarus")}

    msg_none = BriefingMessage(title="t", importance="low", category="other", summary="s")
    _persist_article_entities(dedup_repo=repo, article_id="none", msg=msg_none)
    actor_rows = {e for e in _entities(repo, "none") if e[0] == "actor"}
    assert actor_rows == set()


# ---------- F3: orphan article_entities cleanup ----------


@pytest.mark.unit
def test_cleanup_orphan_entities(repo: RunHistoryRepository) -> None:
    # Arrange: 実在 article a1 + その entity、加えて実在しない ghost の entity
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(run_id=run_id, article_id="a1", title="t", url="u", status="posted"),
    )
    repo.add_article_entities("a1", [("actor", "lazarus"), ("cve", "CVE-2026-1")])
    repo.add_article_entities("ghost", [("actor", "apt41"), ("ioc_ip", "1.2.3.4")])

    # Act
    removed = repo.cleanup_orphan_article_entities()

    # Assert: ghost の entity (実在 article 無し) は削除、a1 は保持
    assert removed == 2
    assert _entities(repo, "ghost") == set()
    assert ("actor", "lazarus") in _entities(repo, "a1")
    assert ("cve", "CVE-2026-1") in _entities(repo, "a1")


@pytest.mark.unit
def test_cleanup_orphan_idempotent(repo: RunHistoryRepository) -> None:
    # orphan 無しなら 0 件、再実行しても安全
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(run_id=run_id, article_id="a1", title="t", url="u", status="posted"),
    )
    repo.add_article_entities("a1", [("actor", "lazarus")])
    assert repo.cleanup_orphan_article_entities() == 0
    assert repo.cleanup_orphan_article_entities() == 0
    assert ("actor", "lazarus") in _entities(repo, "a1")
