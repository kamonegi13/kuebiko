"""割当のプールの頭打ちの記録と、考慮した記事の真の件数。

2026-11-20、docs/synthesis_length_gate.md §2.6。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import src.assessment.stateful as stateful
from src.storage.run_history import RunHistoryRepository


def _insert(repo: RunHistoryRepository, rows: list[tuple[str, str, str, str]]) -> None:
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (id, started_at, pipeline, dry_run, status)"
            " VALUES (1, '2026-11-18T00:00:00+00:00', 'rss', 0, 'done')"
        )
        for aid, status, importance, created in rows:
            conn.execute(
                "INSERT INTO articles (run_id, article_id, url, title, feed_title, status,"
                " importance, created_at) VALUES (1, ?, ?, ?, 'f', ?, ?, ?)",
                (aid, f"https://kuebiko.example/{aid}", aid, status, importance, created),
            )


def test_considered_count_counts_window_high_and_medium_posted(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    _insert(
        repo,
        [
            ("a1", "posted", "high", "2026-11-18T00:00:00+00:00"),
            ("a2", "posted", "medium", "2026-11-19T00:00:00+00:00"),
            ("a3", "posted", "low", "2026-11-19T00:00:00+00:00"),  # low は数えない
            ("a4", "extract_failed", "high", "2026-11-19T00:00:00+00:00"),  # 未配信
            ("a5", "posted", "high", "2026-11-01T00:00:00+00:00"),  # 窓の外
        ],
    )

    n = stateful.considered_article_count(
        repo, since_iso="2026-11-17T00:00:00+00:00", until_iso="2026-11-20T00:00:00+00:00"
    )

    assert n == 2


def test_pool_capped_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(stateful, "_log", log)
    monkeypatch.setattr(stateful, "_STATEFUL_POOL_LIMIT", 3)
    pool = [{"importance": "high"}, {"importance": "medium"}, {"importance": "medium"}]

    stateful.log_pool_capped(pool, considered=10, period_type="weekly")

    log.warning.assert_called_once()
    _, kw = log.warning.call_args
    assert kw["dropped"] == 7 and kw["total"] == 10 and kw["pool_high"] == 1


@pytest.mark.parametrize(("pool_size", "considered"), [(2, 10), (3, 3)])
def test_pool_not_capped_is_quiet(
    monkeypatch: pytest.MonkeyPatch, pool_size: int, considered: int
) -> None:
    log = MagicMock()
    monkeypatch.setattr(stateful, "_log", log)
    monkeypatch.setattr(stateful, "_STATEFUL_POOL_LIMIT", 3)

    stateful.log_pool_capped(
        [{"importance": "high"}] * pool_size, considered=considered, period_type="daily"
    )

    log.warning.assert_not_called()
