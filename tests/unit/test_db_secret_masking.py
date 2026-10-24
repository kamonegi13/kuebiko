"""例外文などの自由文を DB に書く経路が、保存前に機密をマスクすること (CLAUDE.md §4)。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository, RunRecord

# 実在しない形だけのダミー (fixture 規約: 実値を書かない)
_HOOK = "https://discord.com/api/webhooks/123456789/AbCdEf-dummy_token"
_HOOK_APP = "https://discordapp.com/api/webhooks/987654321/ZzYyXx-dummy"
_ERR = f"HTTPStatusError: 404 for url '{_HOOK}'"


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "mask.db")


def _start(repo: RunHistoryRepository, note: str | None = None) -> int:
    return repo.start_run(
        RunRecord(
            started_at=datetime.now(UTC),
            pipeline="daily",
            dry_run=True,
            triggered_by="manual",
            note=note,
        )
    )


def _assert_clean(text: str) -> None:
    assert "/api/webhooks/" not in text
    assert "dummy" not in text


def test_finish_run_masks_webhook_in_note(repo: RunHistoryRepository) -> None:
    run_id = _start(repo)
    repo.finish_run(
        run_id,
        status="failed",
        finished_at=datetime.now(UTC),
        total_fetched=0,
        summarized=0,
        posted=0,
        marked_read=0,
        error_count=1,
        note=_ERR,
    )
    with repo._connect() as conn:
        note = str(conn.execute("SELECT note FROM runs WHERE id = ?", (run_id,)).fetchone()[0])
    _assert_clean(note)
    assert "404" in note


def test_start_run_masks_webhook_in_note(repo: RunHistoryRepository) -> None:
    run_id = _start(repo, note=_ERR)
    with repo._connect() as conn:
        note = str(conn.execute("SELECT note FROM runs WHERE id = ?", (run_id,)).fetchone()[0])
    _assert_clean(note)


def test_start_run_keeps_none_note(repo: RunHistoryRepository) -> None:
    run_id = _start(repo, note=None)
    with repo._connect() as conn:
        assert conn.execute("SELECT note FROM runs WHERE id = ?", (run_id,)).fetchone()[0] is None


def test_append_log_line_masks_webhook(repo: RunHistoryRepository) -> None:
    run_id = _start(repo)
    repo.append_log_line(run_id, f"post failed {_HOOK_APP}")
    lines = repo.get_log_lines(run_id)
    assert lines
    _assert_clean(str(lines[0]))


def test_record_job_run_masks_detail(repo: RunHistoryRepository) -> None:
    repo.record_job_run("job-x", status="failed", detail=_ERR)
    detail = repo.get_job_last_runs()["job-x"]["detail"]
    _assert_clean(detail)
    with repo._connect() as conn:
        hist = str(conn.execute("SELECT detail FROM job_run_log").fetchone()[0])
    _assert_clean(hist)


def test_record_ops_notice_masks_title_and_body(repo: RunHistoryRepository) -> None:
    repo.record_ops_notice(title=_ERR, body=_ERR, sent=False)
    row = repo.list_ops_notices()[0]
    _assert_clean(str(row["title"]))
    _assert_clean(str(row["body"]))


def test_bearer_token_is_masked(repo: RunHistoryRepository) -> None:
    repo.record_job_run("job-y", status="failed", detail="401 Authorization: Bearer abc123dummy")
    assert "abc123dummy" not in repo.get_job_last_runs()["job-y"]["detail"]


def test_scheme_less_webhook_path_in_requests_error_is_masked(
    repo: RunHistoryRepository,
) -> None:
    # requests の ConnectionError は host と path を分けて出す (URL に scheme が無い)
    err = (
        "HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded "
        "with url: /api/webhooks/123456789/AbCdEf-dummy_token (Caused by X)"
    )
    repo.record_job_run("job-z", status="failed", detail=err)
    detail = repo.get_job_last_runs()["job-z"]["detail"]
    _assert_clean(detail)
    assert "Max retries exceeded" in detail


def test_article_failure_reason_masks_webhook(repo: RunHistoryRepository) -> None:
    from src.storage.records import ArticleRecord

    run_id = _start(repo)
    fields = ArticleRecord.model_fields
    assert "failure_reason" in fields
    rec = ArticleRecord.model_validate(
        {
            "run_id": run_id,
            "article_id": "a1",
            "title": "t",
            "url": "https://example.com/a",
            "status": "post_failed",
            "failure_reason": _ERR,
        }
    )
    repo.add_article(rec)
    with repo._connect() as conn:
        reason = str(conn.execute("SELECT failure_reason FROM articles").fetchone()[0])
    _assert_clean(reason)
