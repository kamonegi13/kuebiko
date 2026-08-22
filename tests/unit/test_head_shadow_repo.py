"""較正格子 ロードマップ C: head_shadow repo (蒸留ヘッドのシャドー推論記録) のテスト。

ヘッドの予測と同時点の triage LLM 判定の突合を記録するのみのシャドー計測 —
本番配信への影響が無いことは repo 層のテストでは扱わない (呼び出し側の責務)。
ここでは record→stats の往復、url_hash 冪等性、disagree_cutoff 集計、
retention purge のみを検証する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.repo_head_shadow import HeadShadowRecord
from src.storage.run_history import RunHistoryRepository


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "head_shadow.db")


def _record(
    *,
    url_hash: str,
    article_id: str = "a1",
    head_importance: str = "high",
    triage_importance: str | None = "high",
    triage_kept: bool = True,
    triage_error: bool = False,
    disagree_cutoff: bool = False,
) -> HeadShadowRecord:
    return HeadShadowRecord(
        url_hash=url_hash,
        article_id=article_id,
        head_importance=head_importance,
        head_importance_probs=json.dumps({"high": 0.8, "medium": 0.15, "low": 0.05}),
        triage_kept=triage_kept,
        artifact_version="v0",
        embedding_model="snowflake-arctic-embed2",
        triage_importance=triage_importance,
        triage_error=triage_error,
        disagree_cutoff=disagree_cutoff,
    )


class TestRecordAndStats:
    def test_record_then_stats_roundtrip(self, repo: RunHistoryRepository) -> None:
        # Arrange
        records = [
            _record(url_hash="h1", head_importance="high", triage_importance="high"),
            _record(url_hash="h2", head_importance="low", triage_importance="medium"),
        ]

        # Act
        inserted = repo.record_head_shadow(records)
        stats = repo.head_shadow_stats(days=7)

        # Assert
        assert inserted == 2
        assert stats.total == 2
        assert stats.agree_with_triage == 1  # h1 のみ head==triage
        assert stats.by_head_importance == {"high": 1, "low": 1}

    def test_empty_records_is_a_noop(self, repo: RunHistoryRepository) -> None:
        # Act
        inserted = repo.record_head_shadow([])

        # Assert
        assert inserted == 0
        assert repo.head_shadow_stats(days=7).total == 0

    def test_url_hash_collision_is_ignored_not_duplicated(self, repo: RunHistoryRepository) -> None:
        # Arrange
        first = _record(url_hash="dup", article_id="a1", head_importance="high")
        second = _record(url_hash="dup", article_id="a1", head_importance="low")

        # Act: 同一 url_hash を 2 回挿入 (再実行想定)
        repo.record_head_shadow([first])
        repo.record_head_shadow([second])

        # Assert: 1 行のまま (後発は無視される)
        stats = repo.head_shadow_stats(days=7)
        assert stats.total == 1
        assert stats.by_head_importance == {"high": 1}

    def test_disagree_cutoff_is_counted(self, repo: RunHistoryRepository) -> None:
        # Arrange
        records = [
            _record(url_hash="h1", disagree_cutoff=True, triage_kept=False),
            _record(url_hash="h2", disagree_cutoff=False, triage_kept=True),
        ]

        # Act
        repo.record_head_shadow(records)
        stats = repo.head_shadow_stats(days=7)

        # Assert
        assert stats.disagree_cutoff == 1

    def test_triage_error_is_counted(self, repo: RunHistoryRepository) -> None:
        # Arrange
        records = [
            _record(url_hash="h1", triage_error=True),
            _record(url_hash="h2", triage_error=False),
        ]

        # Act
        repo.record_head_shadow(records)
        stats = repo.head_shadow_stats(days=7)

        # Assert
        assert stats.triage_error == 1

    def test_stats_window_excludes_older_than_days(self, repo: RunHistoryRepository) -> None:
        # Arrange: 1 件を挿入後、created_at を窓外 (30 日前) へ細工する
        repo.record_head_shadow([_record(url_hash="old")])
        old_iso = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        with repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE head_shadow SET created_at = ? WHERE url_hash = 'old'",
                (old_iso,),
            )

        # Act
        stats = repo.head_shadow_stats(days=7)

        # Assert
        assert stats.total == 0


class TestPurge:
    def test_purge_keeps_recent_and_drops_old(self, repo: RunHistoryRepository) -> None:
        # Arrange
        repo.record_head_shadow(
            [
                _record(url_hash="recent"),
                _record(url_hash="old"),
            ]
        )
        old_iso = (datetime.now(UTC) - timedelta(days=200)).isoformat()
        with repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE head_shadow SET created_at = ? WHERE url_hash = 'old'",
                (old_iso,),
            )

        # Act
        deleted = repo.purge_head_shadow(days=180)

        # Assert
        assert deleted == 1
        with repo._connect() as conn:  # noqa: SLF001
            remaining = conn.execute("SELECT url_hash FROM head_shadow").fetchall()
        assert [r["url_hash"] for r in remaining] == ["recent"]

    def test_purge_removes_nothing_when_all_rows_are_recent(
        self, repo: RunHistoryRepository
    ) -> None:
        # Arrange
        repo.record_head_shadow([_record(url_hash="h1"), _record(url_hash="h2")])

        # Act
        deleted = repo.purge_head_shadow(days=180)

        # Assert
        assert deleted == 0
        assert repo.head_shadow_stats(days=7).total == 2
