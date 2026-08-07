"""actor entity retro-cleanup (src/ui/services/entity_retro_cleanup.py) のテスト。

辞書 curation で照合できなくなった行の削除と、3 つの安全弁 (照合成立 /
構造化ソース / 本文なし検証不能) の維持を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.services.entity_retro_cleanup import cleanup_stale_actor_entities

# 現行辞書の想定: ghost_bear は識別名のみ (一般語 alias は除去済み)
_REGISTRY = ActorAliasRegistry(
    actors=(
        ActorAlias(id="ghost_bear", canonical="Ghost Bear", aliases=("GhostBear APT",)),
        ActorAlias(id="lazarus", canonical="Lazarus Group", aliases=("HIDDEN COBRA",)),
    )
)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "cleanup.db")


def _add(
    repo: RunHistoryRepository,
    aid: str,
    *,
    title: str = "title",
    summary: str | None = None,
    body: str | None = None,
    feed_title: str = "Some Feed",
    actors: list[str] | None = None,
) -> None:
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=title,
            url=f"u-{aid}",
            feed_title=feed_title,
            status="posted",
            summary=summary,
            created_at=datetime.now(UTC),
        ),
    )
    if body is not None:
        repo.update_article_body(aid, body)
    if actors:
        repo.add_article_entities(aid, [("actor", a) for a in actors])


def _actor_values(repo: RunHistoryRepository, aid: str) -> set[str]:
    return {v for t, v in repo.get_entities_by_article(aid) if t == "actor"}


def test_stale_entity_is_deleted(repo: RunHistoryRepository) -> None:
    # 本文のどこにも Ghost Bear の現行名が無い (旧一般語 alias 期の残骸を想定)
    _add(repo, "a1", body="A generic security news article.", actors=["ghost_bear"])

    stats = cleanup_stale_actor_entities(repo, registry=_REGISTRY)

    assert stats.deleted == 1
    assert stats.deleted_by_actor == {"ghost_bear": 1}
    assert _actor_values(repo, "a1") == set()


def test_matched_entity_is_kept(repo: RunHistoryRepository) -> None:
    _add(repo, "a1", body="Ghost Bear compromised the ministry.", actors=["ghost_bear"])

    stats = cleanup_stale_actor_entities(repo, registry=_REGISTRY)

    assert stats.deleted == 0
    assert stats.kept_matched == 1
    assert _actor_values(repo, "a1") == {"ghost_bear"}


def test_title_or_summary_match_is_kept(repo: RunHistoryRepository) -> None:
    # 本文には無いが要約に現行 alias がある → 維持 (要約表示で検証可能)
    _add(
        repo,
        "a1",
        summary="HIDDEN COBRA の新しい活動を確認",
        body="the DPRK operation continues.",
        actors=["lazarus"],
    )

    stats = cleanup_stale_actor_entities(repo, registry=_REGISTRY)

    assert stats.deleted == 0
    assert _actor_values(repo, "a1") == {"lazarus"}


def test_structured_leak_feed_is_exempt(repo: RunHistoryRepository) -> None:
    # ransomware.live の actor は slug 由来 (本文照合対象外) — 本文に名前が無くても維持
    _add(
        repo,
        "leak1",
        title="ghostbear: Some Victim Corp (JP)",
        feed_title="Ransomware.live",
        body="victim description only",
        actors=["ghost_bear"],
    )

    stats = cleanup_stale_actor_entities(repo, registry=_REGISTRY)

    assert stats.deleted == 0
    assert stats.kept_structured == 1
    assert _actor_values(repo, "leak1") == {"ghost_bear"}


def test_no_body_anywhere_is_unverifiable_and_kept(repo: RunHistoryRepository) -> None:
    # 90 日 purge 後を想定: 本文なし + title/summary 不一致 → 検証不能として残す
    _add(repo, "old1", body=None, actors=["ghost_bear"])

    stats = cleanup_stale_actor_entities(repo, registry=_REGISTRY)

    assert stats.deleted == 0
    assert stats.kept_unverifiable == 1
    assert _actor_values(repo, "old1") == {"ghost_bear"}


def test_unresolved_id_is_deleted(repo: RunHistoryRepository) -> None:
    _add(repo, "a1", body="whatever", actors=["APT-X"])

    stats = cleanup_stale_actor_entities(repo, registry=_REGISTRY)

    assert stats.deleted == 1
    assert stats.delete_candidates[0][2] == "id_unresolved"


def test_actor_ids_filter_scopes_cleanup(repo: RunHistoryRepository) -> None:
    # ghost_bear と lazarus の両方が stale だが、対象を ghost_bear に限定
    _add(repo, "a1", body="nothing relevant", actors=["ghost_bear", "lazarus"])

    stats = cleanup_stale_actor_entities(repo, registry=_REGISTRY, actor_ids=["ghost_bear"])

    assert stats.deleted == 1
    assert _actor_values(repo, "a1") == {"lazarus"}


def test_dry_run_lists_candidates_without_deleting(repo: RunHistoryRepository) -> None:
    _add(repo, "a1", body="nothing relevant", actors=["ghost_bear"])

    stats = cleanup_stale_actor_entities(repo, registry=_REGISTRY, apply=False)

    assert stats.deleted == 0
    assert len(stats.delete_candidates) == 1
    assert _actor_values(repo, "a1") == {"ghost_bear"}
