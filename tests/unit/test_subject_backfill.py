"""犯行声明突合による主体の決定論補完 (subject_backfill, §13 対処 A) のテスト。

tests/unit/test_tuning_labels.py の _seed_article ヘルパの形を踏襲する
(runs → articles → article_entities の直接 seed)。突合パラメータ (時間窓・
generic org denylist) は label_harvest 側と共有 (SSoT) のため、ここでは
「既存主体を上書きしないこと」「冪等性」「dry_run で DB が変わらないこと」の
subject_backfill 固有の振る舞いを中心に検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.cti.subject_actor import SOURCE_FEED_MATCH
from src.cti.subject_backfill import run_subject_backfill
from src.storage.run_history import RunHistoryRepository

_NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "subject_backfill.db")


def _seed_article(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    subject_actor_source: str = "none",
    subject_actor_ids: str = "",
    body_len: int = 600,
    article_type: str = "",
    victim_orgs: tuple[str, ...] = (),
    created_at: datetime = _NOW,
    published_at: datetime | None = None,
    title: str | None = None,
) -> None:
    """収穫の入力 (articles + article_entities) を直接 seed する (test 専用)。"""
    iso = created_at.isoformat()
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (started_at, pipeline, dry_run, status) VALUES (?, 't', 0, 'done')",
            (iso,),
        )
        rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (run_id, article_id, title, url, status, created_at,"
            " subject_actor_source, subject_actor_ids, body, article_type, published_at)"
            " VALUES (?,?,?,?, 'posted', ?, ?, ?, ?, ?, ?)",
            (
                rid,
                article_id,
                title if title is not None else f"title-{article_id}",
                f"https://kuebiko.example/{article_id}",
                iso,
                subject_actor_source,
                subject_actor_ids,
                "x" * body_len,
                article_type,
                published_at.isoformat() if published_at else None,
            ),
        )
        for org in victim_orgs:
            conn.execute(
                "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
                " VALUES (?,?,?,?)",
                (article_id, "victim_org", org, iso),
            )


def _article_row(repo: RunHistoryRepository, article_id: str) -> dict[str, object]:
    with repo._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT subject_actor_ids, subject_actor_source FROM articles WHERE article_id = ?",
            (article_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


class TestSubjectBackfill:
    def test_fills_empty_subject_within_window(self, repo: RunHistoryRepository) -> None:
        # Arrange: 犯行声明 (feed 帰属) と、同一 victim_org を 1 日後に報じた
        # 主体未確定のニュース記事
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("acme corp",),  # 大文字小文字ゆれは吸収される
            created_at=_NOW - timedelta(days=1),
        )
        # Act
        result = run_subject_backfill(repo, now=_NOW)
        # Assert
        assert result.filled == 1
        assert result.skipped_conflict == 0
        assert result.already_attributed == 0
        row = _article_row(repo, "news1")
        assert row["subject_actor_ids"] == "qilin"
        assert row["subject_actor_source"] == SOURCE_FEED_MATCH

    def test_does_not_overwrite_existing_subject(self, repo: RunHistoryRepository) -> None:
        # Arrange: 一致する声明はあるが、記事には既に (別経路の) 主体が入っている
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="news1",
            subject_actor_source="llm",
            subject_actor_ids="akira",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        # Act
        result = run_subject_backfill(repo, now=_NOW)
        # Assert
        assert result.already_attributed == 1
        assert result.filled == 0
        row = _article_row(repo, "news1")
        assert row["subject_actor_ids"] == "akira"
        assert row["subject_actor_source"] == "llm"

    def test_conflicting_claims_are_not_written(self, repo: RunHistoryRepository) -> None:
        # Arrange: 同一 org に別ギャングの声明が窓内に 2 つ → 正解を決められない
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="claim2",
            subject_actor_source="feed",
            subject_actor_ids="akira",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=3),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        # Act
        result = run_subject_backfill(repo, now=_NOW)
        # Assert
        assert result.filled == 0
        assert result.skipped_conflict == 1
        row = _article_row(repo, "news1")
        assert row["subject_actor_ids"] == ""

    def test_outside_window_is_not_matched(self, repo: RunHistoryRepository) -> None:
        # Arrange: 声明が窓 (±5 日) の外
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=20),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        # Act
        result = run_subject_backfill(repo, now=_NOW)
        # Assert
        assert result.filled == 0
        assert result.skipped_conflict == 0
        assert result.already_attributed == 0
        row = _article_row(repo, "news1")
        assert row["subject_actor_ids"] == ""

    def test_generic_org_names_are_excluded(self, repo: RunHistoryRepository) -> None:
        # Arrange: 一般語の組織名は同名衝突の誤結合源 — 突合キーにしない
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Hospital",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("hospital",),
            created_at=_NOW - timedelta(days=1),
        )
        # Act
        result = run_subject_backfill(repo, now=_NOW)
        # Assert
        assert result.filled == 0
        row = _article_row(repo, "news1")
        assert row["subject_actor_ids"] == ""

    def test_dry_run_does_not_modify_db(self, repo: RunHistoryRepository) -> None:
        # Arrange
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        # Act
        result = run_subject_backfill(repo, now=_NOW, dry_run=True)
        # Assert: 候補数は報告するが DB は変更しない
        assert result.filled == 1
        row = _article_row(repo, "news1")
        assert row["subject_actor_ids"] == ""
        assert row["subject_actor_source"] == "none"

    def test_rerun_is_idempotent(self, repo: RunHistoryRepository) -> None:
        # Arrange
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        # Act: 1 回目で埋まり、2 回目は既に埋まっているので追加の書込は起きない
        first = run_subject_backfill(repo, now=_NOW)
        second = run_subject_backfill(repo, now=_NOW)
        # Assert
        assert first.filled == 1
        assert second.filled == 0
        assert second.already_attributed == 1
        row = _article_row(repo, "news1")
        assert row["subject_actor_ids"] == "qilin"
        assert row["subject_actor_source"] == SOURCE_FEED_MATCH
