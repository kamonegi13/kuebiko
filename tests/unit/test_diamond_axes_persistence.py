"""Phase Diamond-Axes: socio_political_intent / technical_axis_summary の DB 永続化テスト。

SQLite migration (ALTER TABLE で列追加) → add_article INSERT → list_articles の
_row_to_article round-trip を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.run_history import (
    ArticleRecord,
    RunHistoryRepository,
    RunRecord,
)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "diamond.db")


def _now() -> datetime:
    return datetime.now(UTC)


def _add(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    intent: str | None,
    rationale: str | None = None,
    technical: str | None = None,
) -> None:
    run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title="t",
            url=f"https://example.com/{article_id}",
            feed_title="f",
            status="posted",
            socio_political_intent=intent,
            socio_political_rationale=rationale,
            technical_axis_summary=technical,
            created_at=_now(),
        ),
    )


@pytest.mark.unit
def test_socio_political_round_trip(repo: RunHistoryRepository) -> None:
    # Arrange
    _add(
        repo,
        article_id="a",
        intent="espionage",
        rationale="中国系 APT が知財窃取",
        technical="ScreenConnect 経由で侵入し Cobalt Strike を C2 運用",
    )
    # Act
    rows = repo.list_articles(limit=10)
    # Assert
    assert len(rows) == 1
    assert rows[0].socio_political_intent == "espionage"
    assert rows[0].socio_political_rationale == "中国系 APT が知財窃取"
    assert "Cobalt Strike" in (rows[0].technical_axis_summary or "")


@pytest.mark.unit
def test_null_axes_round_trip(repo: RunHistoryRepository) -> None:
    # geopolitical 等で軸が判定不能 (None) でもクラッシュせず NULL を保持
    _add(repo, article_id="a", intent=None)
    rows = repo.list_articles(limit=10)
    assert rows[0].socio_political_intent is None
    assert rows[0].socio_political_rationale is None
    assert rows[0].technical_axis_summary is None


@pytest.mark.unit
def test_multiple_intents_distinct(repo: RunHistoryRepository) -> None:
    _add(repo, article_id="a", intent="espionage")
    _add(repo, article_id="b", intent="financial")
    _add(repo, article_id="c", intent="prepositioning")
    rows = repo.list_articles(limit=10)
    intents = {r.article_id: r.socio_political_intent for r in rows}
    assert intents == {"a": "espionage", "b": "financial", "c": "prepositioning"}
