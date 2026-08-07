"""D4 GC-root 方式の本文 retention 免除のテスト (2026-07-26)。

永続記録 (行動史/台帳/任務/triage/辞書) から参照される記事の本文は purge しない。
root 5 種それぞれの保護と、root なし記事の従来どおりの purge を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "roots.db")


def _add_old_article(
    repo: RunHistoryRepository,
    aid: str,
    *,
    subject: str | None = None,
    importance: str | None = None,
    victim_country: str | None = None,
    channel: str | None = None,
) -> None:
    created = datetime.now(UTC) - timedelta(days=200)
    rid = repo.start_run(RunRecord(started_at=created, pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=f"t-{aid}",
            url=f"https://x.example/{aid}",
            status="posted",
            subject_actor_ids=subject,
            importance=importance,
            victim_country_iso=victim_country,
            posted_channel=channel,
            created_at=created,
        ),
    )
    repo.update_article_body(aid, f"body-{aid}", fetched_at=created)


def _body(repo: RunHistoryRepository, aid: str) -> str | None:
    return repo.get_article_body(aid)


def test_no_root_article_is_purged(repo: RunHistoryRepository) -> None:
    _add_old_article(repo, "plain")
    assert repo.purge_article_bodies_older_than(90) == 1
    assert _body(repo, "plain") is None


def test_subject_root_is_kept(repo: RunHistoryRepository) -> None:
    _add_old_article(repo, "subj", subject="qilin")
    assert repo.purge_article_bodies_older_than(90) == 0
    assert _body(repo, "subj") == "body-subj"


def test_high_importance_root_is_kept(repo: RunHistoryRepository) -> None:
    _add_old_article(repo, "high", importance="high")
    repo.purge_article_bodies_older_than(90)
    assert _body(repo, "high") == "body-high"


def test_japan_roots_are_kept(repo: RunHistoryRepository) -> None:
    _add_old_article(repo, "jpv", victim_country="JP")
    _add_old_article(repo, "jpc", channel="japan_watch")
    repo.purge_article_bodies_older_than(90)
    assert _body(repo, "jpv") == "body-jpv"
    assert _body(repo, "jpc") == "body-jpc"


def test_actor_mention_root_is_kept(repo: RunHistoryRepository) -> None:
    _add_old_article(repo, "ment")
    repo.add_article_entities("ment", [("actor", "qilin")])
    _add_old_article(repo, "prov")
    repo.add_article_entities("prov", [("actor_provisional", "newgroup")])
    repo.purge_article_bodies_older_than(90)
    assert _body(repo, "ment") == "body-ment"
    assert _body(repo, "prov") == "body-prov"


def test_ledger_evidence_root_is_kept(repo: RunHistoryRepository) -> None:
    _add_old_article(repo, "evid")
    with repo._connect() as conn:  # noqa: SLF001 — テストでの台帳採用の再現
        conn.execute(
            "INSERT INTO situation_evidence (situation_id, article_id, added_at) "
            "VALUES ('sit-1', 'evid', '2026-07-01T00:00:00+00:00')"
        )
    repo.purge_article_bodies_older_than(90)
    assert _body(repo, "evid") == "body-evid"


def test_root_check_is_article_grained_across_runs(repo: RunHistoryRepository) -> None:
    """run 横断の重複行の 1 行にでも root があれば記事全体が保護される。"""
    created = datetime.now(UTC) - timedelta(days=200)
    for i, imp in enumerate(("low", "high")):
        rid = repo.start_run(RunRecord(started_at=created, pipeline="x", dry_run=False))
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id="dup",
                title="t",
                url=f"https://x.example/dup{i}",
                status="posted",
                importance=imp,
                created_at=created,
            ),
        )
    repo.update_article_body("dup", "body-dup", fetched_at=created)
    assert repo.purge_article_bodies_older_than(90) == 0
    assert _body(repo, "dup") == "body-dup"


def test_recent_no_root_article_untouched(repo: RunHistoryRepository) -> None:
    created = datetime.now(UTC) - timedelta(days=5)
    rid = repo.start_run(RunRecord(started_at=created, pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id="fresh",
            title="t",
            url="https://x.example/fresh",
            status="posted",
            created_at=created,
        ),
    )
    repo.update_article_body("fresh", "body-fresh", fetched_at=created)
    repo.purge_article_bodies_older_than(90)
    assert _body(repo, "fresh") == "body-fresh"
