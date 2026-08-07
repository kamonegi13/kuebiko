"""src.storage.run_history のライブログ機能テスト (Phase 1.5b)。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import (
    MAX_LOG_LINE_BYTES,
    MAX_LOG_LINES_PER_RUN,
    RunHistoryRepository,
    RunRecord,
)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "logs.db")


def _now() -> datetime:
    return datetime.now(UTC)


def _start(repo: RunHistoryRepository) -> int:
    return repo.start_run(
        RunRecord(started_at=_now(), pipeline="daily", dry_run=True, triggered_by="manual"),
    )


class TestAppendLogLine:
    def test_seq_starts_at_one_and_increments(self, repo: RunHistoryRepository) -> None:
        run_id = _start(repo)
        assert repo.append_log_line(run_id, "first") == 1
        assert repo.append_log_line(run_id, "second") == 2
        assert repo.append_log_line(run_id, "third") == 3

    def test_each_run_has_independent_seq(self, repo: RunHistoryRepository) -> None:
        run_a = _start(repo)
        run_b = _start(repo)
        assert repo.append_log_line(run_a, "a1") == 1
        assert repo.append_log_line(run_b, "b1") == 1
        assert repo.append_log_line(run_a, "a2") == 2
        assert repo.append_log_line(run_b, "b2") == 2

    def test_log_line_count_is_updated(self, repo: RunHistoryRepository) -> None:
        run_id = _start(repo)
        repo.append_log_line(run_id, "x")
        repo.append_log_line(run_id, "y")
        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.log_line_count == 2
        assert loaded.log_truncated is False

    def test_truncates_long_line(self, repo: RunHistoryRepository) -> None:
        run_id = _start(repo)
        big = "あ" * (MAX_LOG_LINE_BYTES // 3 + 100)  # 3 byte per char in UTF-8
        seq = repo.append_log_line(run_id, big)
        assert seq == 1
        lines = repo.get_log_lines(run_id)
        assert len(lines) == 1
        # サフィックスが付いていること、元の長さより短くなっていること
        assert lines[0].line.endswith("[truncated]")
        assert len(lines[0].line.encode("utf-8")) <= MAX_LOG_LINE_BYTES

    def test_unknown_run_id_raises(self, repo: RunHistoryRepository) -> None:
        with pytest.raises(ValueError, match="存在しません"):
            repo.append_log_line(9999, "ghost")

    def test_drops_lines_after_max(self, repo: RunHistoryRepository) -> None:
        # MAX_LOG_LINES_PER_RUN を超えたら追加しない (テスト時は短縮するため
        # 直接定数を使うのではなく、5000 行投入して上限挙動を確認)
        run_id = _start(repo)
        # 上限超過テストは行数が大きすぎるので、定数を信じてピン止めだけ確認
        for i in range(MAX_LOG_LINES_PER_RUN):
            repo.append_log_line(run_id, f"line {i}")
        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.log_line_count == MAX_LOG_LINES_PER_RUN
        assert loaded.log_truncated is True

        # 追加分はドロップ (None を返す)
        assert repo.append_log_line(run_id, "overflow") is None
        loaded2 = repo.get_run(run_id)
        assert loaded2 is not None
        assert loaded2.log_line_count == MAX_LOG_LINES_PER_RUN  # 増えていない

        # 末尾の行は警告メッセージ
        lines = repo.get_log_lines(
            run_id,
            from_seq=MAX_LOG_LINES_PER_RUN - 1,
            limit=1,
        )
        assert "log truncated" in lines[0].line


class TestGetLogLines:
    def test_returns_in_order(self, repo: RunHistoryRepository) -> None:
        run_id = _start(repo)
        for i in range(5):
            repo.append_log_line(run_id, f"line-{i}")
        lines = repo.get_log_lines(run_id)
        assert [line.seq for line in lines] == [1, 2, 3, 4, 5]
        assert [line.line for line in lines] == [f"line-{i}" for i in range(5)]

    def test_filters_by_from_seq(self, repo: RunHistoryRepository) -> None:
        run_id = _start(repo)
        for i in range(5):
            repo.append_log_line(run_id, f"line-{i}")
        lines = repo.get_log_lines(run_id, from_seq=2)
        assert [line.seq for line in lines] == [3, 4, 5]

    def test_respects_limit(self, repo: RunHistoryRepository) -> None:
        run_id = _start(repo)
        for i in range(10):
            repo.append_log_line(run_id, f"line-{i}")
        lines = repo.get_log_lines(run_id, limit=3)
        assert len(lines) == 3
        assert [line.seq for line in lines] == [1, 2, 3]

    def test_returns_empty_for_no_logs(self, repo: RunHistoryRepository) -> None:
        run_id = _start(repo)
        assert repo.get_log_lines(run_id) == []


class TestFailDanglingRuns:
    def test_marks_running_as_failed(self, repo: RunHistoryRepository) -> None:
        run_a = _start(repo)
        run_b = _start(repo)
        # run_b は完了させておく
        repo.finish_run(
            run_b,
            status="succeeded",
            finished_at=_now(),
            total_fetched=1,
            summarized=1,
            posted=1,
            marked_read=0,
            error_count=0,
        )
        count = repo.fail_dangling_runs()
        assert count == 1
        loaded_a = repo.get_run(run_a)
        loaded_b = repo.get_run(run_b)
        assert loaded_a is not None and loaded_a.status == "failed"
        assert loaded_a.note is not None
        assert "[recovered after restart]" in loaded_a.note
        assert loaded_b is not None and loaded_b.status == "succeeded"

    def test_returns_zero_when_no_running(self, repo: RunHistoryRepository) -> None:
        assert repo.fail_dangling_runs() == 0


class TestPurgeOldLogs:
    def test_deletes_old_logs(self, repo: RunHistoryRepository) -> None:
        run_id = _start(repo)
        old = _now() - timedelta(days=60)
        new = _now()
        repo.append_log_line(run_id, "old", ts=old)
        repo.append_log_line(run_id, "new", ts=new)
        deleted = repo.purge_old_logs(days=30)
        assert deleted == 1
        remaining = repo.get_log_lines(run_id)
        assert len(remaining) == 1
        assert remaining[0].line == "new"

    def test_returns_zero_when_nothing_to_delete(self, repo: RunHistoryRepository) -> None:
        assert repo.purge_old_logs(days=30) == 0


class TestMigrationFromLegacySchema:
    def test_alter_table_adds_missing_columns(self, tmp_path: Path) -> None:
        db = tmp_path / "legacy.db"
        # 旧スキーマ (log_line_count / log_truncated 列なし) を直接作る
        conn = sqlite3.connect(db)
        try:
            conn.executescript(
                """
                CREATE TABLE runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    pipeline TEXT NOT NULL,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    triggered_by TEXT NOT NULL DEFAULT 'scheduler',
                    status TEXT NOT NULL DEFAULT 'running',
                    total_fetched INTEGER NOT NULL DEFAULT 0,
                    summarized INTEGER NOT NULL DEFAULT 0,
                    posted INTEGER NOT NULL DEFAULT 0,
                    marked_read INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    note TEXT
                );
                INSERT INTO runs (started_at, pipeline, dry_run)
                VALUES ('2026-01-01T00:00:00+00:00', 'daily', 0);
                """,
            )
            conn.commit()
        finally:
            conn.close()

        # 新リポジトリでオープン → ALTER TABLE が走る
        repo = RunHistoryRepository(db_path=db)
        # 既存行が log_line_count=0 / log_truncated=False で読めること
        runs = repo.list_runs(limit=10)
        assert len(runs) == 1
        assert runs[0].log_line_count == 0
        assert runs[0].log_truncated is False
        # 新たに log を追加できること (run_logs テーブルも作成済み)
        repo.append_log_line(runs[0].id or 0, "hello")
        assert len(repo.get_log_lines(runs[0].id or 0)) == 1
