"""flow Phase 3: routing_rule_id / routing_reason の DB 永続化テスト。

記事単位の「なぜこのチャンネルか」を説明するため、route() が決めた
rule_id + 人向け reason を articles に永続化する。SQLite migration
(ALTER TABLE で列追加) → add_article INSERT → get_article / list_articles の
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
    return RunHistoryRepository(db_path=tmp_path / "routing_audit.db")


def _now() -> datetime:
    return datetime.now(UTC)


def _add(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    rule_id: str | None,
    reason: str | None,
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
            posted_channel="alert",
            routing_rule_id=rule_id,
            routing_reason=reason,
            created_at=_now(),
        ),
    )


@pytest.mark.unit
def test_routing_audit_round_trip(repo: RunHistoryRepository) -> None:
    # Arrange
    _add(
        repo,
        article_id="a",
        rule_id="R2.inoreader.alert_japan_critical_apt",
        reason="日本の重要インフラを標的にした APT 活動",
    )
    # Act
    rows = repo.list_articles(limit=10)
    # Assert
    assert len(rows) == 1
    assert rows[0].routing_rule_id == "R2.inoreader.alert_japan_critical_apt"
    assert rows[0].routing_reason == "日本の重要インフラを標的にした APT 活動"


@pytest.mark.unit
def test_routing_audit_get_article(repo: RunHistoryRepository) -> None:
    # detail API は get_article 経由なので単体でも引けることを確認
    _add(repo, article_id="a", rule_id="R5.brief_default", reason="既定の通読チャンネル")
    rec = repo.get_article("a")
    assert rec is not None
    assert rec.routing_rule_id == "R5.brief_default"
    assert rec.routing_reason == "既定の通読チャンネル"


@pytest.mark.unit
def test_routing_audit_null_round_trip(repo: RunHistoryRepository) -> None:
    # route() 非経由 (旧レコード / grok metadata 直指定) は None を保持しクラッシュしない
    _add(repo, article_id="a", rule_id=None, reason=None)
    rec = repo.get_article("a")
    assert rec is not None
    assert rec.routing_rule_id is None
    assert rec.routing_reason is None
