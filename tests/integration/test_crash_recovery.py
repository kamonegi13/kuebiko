"""アプリ起動時のクラッシュ復旧 + retention テスト (Phase 1.5b)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "crash.db")


def test_dangling_run_is_marked_failed_on_restart(tmp_path: Path) -> None:
    """前回 running のまま残った run が、新リポジトリ生成時に failed に倒れる。"""
    db = tmp_path / "crash.db"
    repo1 = RunHistoryRepository(db_path=db)
    run_id = repo1.start_run(
        RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=True),
    )
    # わざと finish せずに別のリポジトリで再オープン (アプリ再起動を模す)
    repo2 = RunHistoryRepository(db_path=db)
    recovered = repo2.fail_dangling_runs()
    assert recovered == 1
    loaded = repo2.get_run(run_id)
    assert loaded is not None
    assert loaded.status == "failed"
    assert loaded.note is not None
    assert "[recovered after restart]" in loaded.note


def test_retention_purges_old_logs(repo: RunHistoryRepository) -> None:
    run_id = repo.start_run(
        RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=True),
    )
    very_old = datetime.now(UTC) - timedelta(days=90)
    new = datetime.now(UTC)
    repo.append_log_line(run_id, "old1", ts=very_old)
    repo.append_log_line(run_id, "old2", ts=very_old)
    repo.append_log_line(run_id, "new1", ts=new)

    purged = repo.purge_old_logs(days=30)
    assert purged == 2
    remaining = repo.get_log_lines(run_id)
    assert [line.line for line in remaining] == ["new1"]
