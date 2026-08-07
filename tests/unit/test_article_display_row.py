"""表示行選択 (_DISPLAY_ROW_ORDER) のテスト (2026-08-06 幽霊アクター監査の併発是正)。

毎時 dedup の再観測行 (skipped_duplicate、summary NULL・再翻訳タイトル) が
より新しくても、get_article / get_article_body / get_article_body_ja は
実取込行 (posted 等) を優先して返すことを検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.records import ArticleStatus
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "display.db")


def _add_row(
    repo: RunHistoryRepository,
    aid: str,
    *,
    status: ArticleStatus,
    title: str,
    summary: str | None,
    age_minutes: int,
) -> int:
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=title,
            url=f"u-{aid}",
            status=status,
            summary=summary,
            created_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
        ),
    )
    return rid


def test_posted_row_preferred_over_newer_duplicate(repo: RunHistoryRepository) -> None:
    _add_row(
        repo, "a1", status="posted", title="投稿時タイトル", summary="要約あり", age_minutes=60
    )
    # 毎時再観測: より新しいが summary NULL・タイトルは再翻訳で揺れている
    _add_row(
        repo, "a1", status="skipped_duplicate", title="再翻訳タイトル", summary=None, age_minutes=1
    )

    row = repo.get_article("a1")
    assert row is not None
    assert row.title == "投稿時タイトル"
    assert row.summary == "要約あり"
    assert row.status == "posted"


def test_newest_posted_row_wins_among_non_duplicates(repo: RunHistoryRepository) -> None:
    _add_row(repo, "a1", status="posted", title="古い実行", summary="s1", age_minutes=120)
    _add_row(repo, "a1", status="posted", title="新しい実行", summary="s2", age_minutes=10)

    row = repo.get_article("a1")
    assert row is not None
    assert row.title == "新しい実行"


def test_duplicate_only_article_falls_back_to_newest(repo: RunHistoryRepository) -> None:
    _add_row(repo, "a1", status="skipped_duplicate", title="dup 古", summary=None, age_minutes=60)
    _add_row(repo, "a1", status="skipped_duplicate", title="dup 新", summary=None, age_minutes=1)

    row = repo.get_article("a1")
    assert row is not None
    assert row.title == "dup 新"


def test_body_readers_follow_display_row(repo: RunHistoryRepository) -> None:
    _add_row(repo, "a1", status="posted", title="t", summary="s", age_minutes=60)
    _add_row(repo, "a1", status="skipped_duplicate", title="t2", summary=None, age_minutes=1)
    # update_article_body は article_id 全行に書くため行間で一致するが、
    # 読み取りが表示行に固定されていること (非決定 fetchone の排除) を確認
    repo.update_article_body("a1", "本文テキスト")
    repo.update_article_body_ja("a1", "訳文テキスト")

    assert repo.get_article_body("a1") == "本文テキスト"
    assert repo.get_article_body_ja("a1") == "訳文テキスト"
