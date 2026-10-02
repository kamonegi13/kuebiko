"""triage の落選記録 (2026-10-02)。

落選した記事は articles に行を作らず URL 既読化だけされていたため、誤った落選を後から
確かめる手段が無かった (理由は 30 日で消える run_logs にしか無い)。理由つきで残し、
購読ソースの画面で媒体ごとに読めるようにする。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.storage.repo_triage_rejections import TriageRejectionRow
from src.storage.run_history import RunHistoryRepository


def _row(
    i: int, *, feed_url: str = "https://a.example/feed", reason: str = "無関係"
) -> TriageRejectionRow:
    return TriageRejectionRow(
        article_id=f"rss:{i}",
        url=f"https://a.example/{i}",
        title=f"記事 {i}",
        feed_title="A",
        feed_url=feed_url,
        importance="low",
        reason=reason,
    )


def test_recorded_rejections_are_listed_newest_first(tmp_path: Path) -> None:
    # Arrange
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_triage_rejections([_row(1)], when=datetime.now(UTC) - timedelta(hours=2))
    repo.record_triage_rejections([_row(2, reason="学術論文")])

    # Act
    rows = repo.list_triage_rejections(days=7)

    # Assert
    assert [r.article_id for r in rows] == ["rss:2", "rss:1"]
    assert rows[0].reason == "学術論文"
    assert rows[0].feed_title == "A"


def test_list_filters_by_feed_key(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_triage_rejections([_row(1), _row(2, feed_url="https://b.example/feed")])

    rows = repo.list_triage_rejections(days=7, feed_key="https://b.example/feed")

    assert [r.article_id for r in rows] == ["rss:2"]


def test_counts_by_feed_key_within_window(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_triage_rejections([_row(1), _row(2)])
    repo.record_triage_rejections(
        [_row(3, feed_url="https://b.example/feed")],
        when=datetime.now(UTC) - timedelta(days=40),
    )

    counts = repo.count_triage_rejections_by_feed(days=30)

    assert counts == {"https://a.example/feed": 2}


def test_purge_drops_old_rows(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_triage_rejections([_row(1)], when=datetime.now(UTC) - timedelta(days=200))
    repo.record_triage_rejections([_row(2)])

    purged = repo.purge_triage_rejections(days=180)

    assert purged == 1
    assert [r.article_id for r in repo.list_triage_rejections(days=365)] == ["rss:2"]


def test_empty_input_writes_nothing(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")

    assert repo.record_triage_rejections([]) == 0


def test_api_returns_rejections_for_feed_with_clamped_window(tmp_path: Path) -> None:
    """購読ソース画面の一覧 API。日数と件数は上限で抑える。"""
    from types import SimpleNamespace
    from typing import Any, cast

    from src.ui.api.pages import subscriptions_triage_rejections

    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_triage_rejections([_row(1), _row(2, feed_url="https://b.example/feed")])
    request = cast(Any, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo))))

    body = subscriptions_triage_rejections(
        request, feed_key="https://a.example/feed", days=9999, limit=10
    )

    assert body["days"] == 180
    assert [i["article_id"] for i in body["items"]] == ["rss:1"]
    assert body["items"][0]["reason"] == "無関係"
