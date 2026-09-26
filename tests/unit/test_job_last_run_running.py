"""job_last_run の running 記録 (2026-09-26、ジョブ画面の「実行中」表示)。"""

from __future__ import annotations

from pathlib import Path

from src.storage.run_history import RunHistoryRepository


def test_running_mark_is_overwritten_by_completion(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "rh.db")

    repo.mark_job_running("job-a")
    assert repo.get_job_last_runs()["job-a"]["status"] == "running"

    repo.record_job_run("job-a", status="succeeded", detail="ok")
    assert repo.get_job_last_runs()["job-a"]["status"] == "succeeded"
    # 履歴には完了した実行だけが並ぶ
    assert [r["status"] for r in repo.runs_for_job("job-a")] == ["succeeded"]


def test_dangling_running_is_failed_at_startup(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "rh.db")
    repo.mark_job_running("job-b")
    repo.record_job_run("job-c", status="succeeded")

    assert repo.fail_dangling_job_runs() == 1

    last = repo.get_job_last_runs()
    assert last["job-b"]["status"] == "failed"
    assert "再起動" in last["job-b"]["detail"]
    assert last["job-c"]["status"] == "succeeded"
