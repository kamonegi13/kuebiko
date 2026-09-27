"""週次深掘りの閲覧 API (2026-09-27) — 本文と選んだ記事を週ごとに返す。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import src.ui.api.deep_dives as deep_dives
from src.storage.records import F1SelectionRecord
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


def test_lists_weeks_with_selections_sorted_by_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    rid = repo.start_run(
        RunRecord(started_at=datetime.now(UTC), pipeline="weekly-recap", dry_run=False)
    )
    for aid, title in (("a1", "低い方"), ("a2", "高い方")):
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=aid,
                title=title,
                url=f"https://kuebiko.example/{aid}",
                status="posted",
            )
        )
    repo.record_f1_selections(
        [
            F1SelectionRecord(
                run_id=rid,
                article_id="a1",
                composite_score=2.8,
                pir=3,
                roi=3,
                timeliness=0,
                novelty=5,
            ),
            F1SelectionRecord(
                run_id=rid,
                article_id="a2",
                composite_score=4.6,
                pir=5,
                roi=4,
                timeliness=0,
                novelty=5,
            ),
        ]
    )
    repo.record_weekly_recap(
        run_id=rid, period_label="W1", recap_text="## 主題\n本文", candidate_count=30
    )
    monkeypatch.setattr(deep_dives, "RunHistoryRepository", lambda: repo)

    got = deep_dives.list_deep_dives(weeks=4)

    assert got["total"] == 1
    week = got["items"][0]
    assert week["period_label"] == "W1"
    assert week["candidate_count"] == 30
    assert [s["title"] for s in week["selections"]] == ["高い方", "低い方"]
