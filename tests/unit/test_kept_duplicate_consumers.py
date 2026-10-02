"""重複として分析に残した記事 (status='posted' + duplicate_of、2026-10-02) を数えない消費者。

記事を読む分析 (台帳の証拠・総括) には残すが、件数・順位に効く消費者が同じ出来事を
二重に数えないことを固定する。事象単位で数える仕組みが入るまでの境界。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    r = RunHistoryRepository(db_path=tmp_path / "kept.db")
    rid = r.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    for aid, dup in (("orig", None), ("dup", "orig")):
        r.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=aid,
                title="t",
                url=f"https://kuebiko.example/{aid}",
                status="posted",
                posted_channel=None if dup else "watch",
                importance="high",
                category="incident",
                dedup_key="same-key",
                duplicate_of=dup,
                created_at=datetime.now(UTC),
            )
        )
    return r


def test_pir_evaluation_rows_exclude_kept_duplicates(repo: RunHistoryRepository) -> None:
    from src.pir.evaluator import _load_posted_rows

    rows, _actors = _load_posted_rows(repo=repo)

    assert [r["article_id"] for r in rows] == ["orig"]


def test_followup_prior_count_ignores_kept_duplicates(repo: RunHistoryRepository) -> None:
    count, latest = repo.count_prior_posts_by_dedup_key("same-key", lookback_hours=24)

    assert count == 1
    assert latest is not None and latest.article_id == "orig"
