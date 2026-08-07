"""high_threat_digest (高脅威 Recall 安全網) の unit test。

分類 (importance=high=即時通知) と配信 (web-only=沈黙) の断絶の恒久対処。
alert 未 push の high サイバー脅威を必ず日次ブリーフに載せる Recall 完全性を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.digest.high_threat_digest import (
    HighThreatItem,
    collect_high_threats,
    format_high_threat_compact,
)
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "ht.db")


def _add(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    importance: str = "high",
    category: str = "apt",
    channel: str = "watch",
    victim_iso: str | None = None,
    dedup_key: str | None = None,
    hours_ago: float = 1.0,
) -> None:
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title=f"脅威 {article_id}",
            url=f"https://example.com/{article_id}",
            feed_title="f",
            summary="s",
            importance=importance,
            category=category,
            posted_channel=channel,
            status="posted",
            victim_country_iso=victim_iso,
            dedup_key=dedup_key,
            created_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        ),
    )


class TestCollectHighThreats:
    def test_includes_high_webonly_cyber(self, repo: RunHistoryRepository) -> None:
        # Arrange: web-only の high APT
        _add(repo, article_id="a1", channel="watch", category="apt")

        # Act
        items, total = collect_high_threats(lookback_hours=24, db_path=repo.db_path)

        # Assert
        assert total == 1
        assert items[0].article_id == "a1"

    def test_excludes_alert_pushed(self, repo: RunHistoryRepository) -> None:
        # alert は即応 push 済み → 安全網リストから除外
        _add(repo, article_id="a1", channel="alert", category="apt")

        items, total = collect_high_threats(lookback_hours=24, db_path=repo.db_path)

        assert total == 0
        assert items == []

    def test_excludes_medium_and_low(self, repo: RunHistoryRepository) -> None:
        _add(repo, article_id="m1", importance="medium", channel="watch")
        _add(repo, article_id="l1", importance="low", channel="watch")

        items, _ = collect_high_threats(lookback_hours=24, db_path=repo.db_path)

        assert items == []

    def test_excludes_non_actionable_categories(self, repo: RunHistoryRepository) -> None:
        # geopolitical/policy は状況認識であり「act now 脅威」ではない → 除外
        _add(repo, article_id="g1", category="geopolitical", channel="watch")
        _add(repo, article_id="p1", category="policy", channel="watch")

        items, _ = collect_high_threats(lookback_hours=24, db_path=repo.db_path)

        assert items == []

    def test_dedup_by_key(self, repo: RunHistoryRepository) -> None:
        _add(repo, article_id="a1", dedup_key="same", hours_ago=1)
        _add(repo, article_id="a2", dedup_key="same", hours_ago=2)

        items, total = collect_high_threats(lookback_hours=24, db_path=repo.db_path)

        assert total == 1  # 重複排除

    def test_japan_targeted_sorted_first(self, repo: RunHistoryRepository) -> None:
        # Arrange: 非日本 (新しい) と 日本標的 (古い)
        _add(repo, article_id="us", channel="watch", victim_iso="US", hours_ago=1)
        _add(repo, article_id="jp", channel="japan_watch", victim_iso="JP", hours_ago=5)

        # Act
        items, _ = collect_high_threats(lookback_hours=24, db_path=repo.db_path)

        # Assert: 日本標的が先頭 (created_at が古くても優先)
        assert items[0].article_id == "jp"
        assert items[0].is_japan is True
        assert items[1].is_japan is False

    def test_lookback_window(self, repo: RunHistoryRepository) -> None:
        _add(repo, article_id="recent", hours_ago=1)
        _add(repo, article_id="old", hours_ago=48)

        items, total = collect_high_threats(lookback_hours=24, db_path=repo.db_path)

        assert total == 1
        assert items[0].article_id == "recent"

    def test_limit_and_total(self, repo: RunHistoryRepository) -> None:
        for i in range(15):
            _add(repo, article_id=f"a{i}", dedup_key=f"k{i}", hours_ago=i * 0.1 + 0.1)

        items, total = collect_high_threats(lookback_hours=24, db_path=repo.db_path, limit=12)

        assert total == 15  # 上限適用前
        assert len(items) == 12  # 上限適用後


class TestFormatHighThreatCompact:
    def test_empty_returns_blank(self) -> None:
        assert format_high_threat_compact([], total=0, base_url=None) == ""

    def test_renders_header_and_items(self) -> None:
        items = [
            HighThreatItem("a1", "APT41 が製造業に侵入", "apt", "https://x/1", is_japan=False),
        ]

        out = format_high_threat_compact(items, total=1, base_url=None)

        assert "本日の高脅威" in out
        assert "1 件" in out
        assert "APT41 が製造業に侵入" in out
        assert "(APT)" in out
        assert "<https://x/1>" in out

    def test_japan_flag_and_overflow(self) -> None:
        items = [
            HighThreatItem("jp", "日本の電力会社が標的", "incident", "https://x/jp", is_japan=True),
        ]

        out = format_high_threat_compact(items, total=13, base_url="https://cti.example")

        assert "🇯🇵" in out
        assert "他 12 件" in out
        assert "cti.example/app/news?importance=high" in out
