"""src.cti.entity_dedup のテスト (Step 3, 2026-08-19)。

victim_org × 日付の同一被害組織照合を検証する:
  - build_org_date_index / is_covered_within_window: ransomware_ingest 汎用化部分
  - find_recent_victim_org_duplicate: 投稿直前ゲート用の直接 DB 照合 (24h/表記ゆれ)
  - victim_org_dedup_enabled: env flag によるロールバック
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.cti.entity_dedup import (
    build_org_date_index,
    find_recent_victim_org_duplicate,
    is_covered_within_window,
    victim_org_dedup_enabled,
)
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "entity_dedup.db")


def _now() -> datetime:
    return datetime.now(UTC)


def _seed_posted_article(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    victim_org: str,
    hours_ago: float,
    feed_title: str = "test-feed",
    status: str = "posted",
) -> None:
    run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title=f"{victim_org} breach",
            url=f"https://example.com/{article_id}",
            feed_title=feed_title,
            status=status,  # type: ignore[arg-type]
            category="breach",
            created_at=_now() - timedelta(hours=hours_ago),
        ),
    )
    repo.add_article_entities(article_id, [("victim_org", victim_org)])


class TestBuildOrgDateIndex:
    def test_drops_unparseable_and_missing_dates(self) -> None:
        # Arrange
        rows: list[tuple[str, object]] = [
            ("a", None),
            ("b", "not-a-date"),
            ("c", "2026-06-18T00:00:00"),
        ]

        # Act
        idx = build_org_date_index(rows)

        # Assert
        assert "a" not in idx
        assert "b" not in idx
        assert len(idx["c"]) == 1

    def test_groups_multiple_dates_under_same_org(self) -> None:
        # Arrange / Act
        idx = build_org_date_index(
            [("acme", "2026-06-01"), ("acme", "2026-06-15")],
        )

        # Assert
        assert len(idx["acme"]) == 2


class TestIsCoveredWithinWindow:
    def test_within_window_is_covered(self) -> None:
        # Arrange
        idx = build_org_date_index([("acme kogyo k.k.", datetime(2026, 6, 18))])

        # Act / Assert
        assert (
            is_covered_within_window(idx, "Acme Kogyo K.K.", datetime(2026, 6, 20), window_days=60)
            is True
        )

    def test_outside_window_is_not_covered(self) -> None:
        # Arrange: 半年前の掲載は別被害とみなす
        idx = build_org_date_index([("acme kogyo k.k.", datetime(2025, 12, 1))])

        # Act / Assert
        assert (
            is_covered_within_window(idx, "Acme Kogyo K.K.", datetime(2026, 6, 20), window_days=60)
            is False
        )

    def test_unknown_when_date_falls_back_to_org_match(self) -> None:
        # Arrange
        idx = build_org_date_index([("acme", datetime(2026, 6, 18))])

        # Act / Assert: 日付不明 → org 一致のみで保守的に True
        assert is_covered_within_window(idx, "acme", None, window_days=60) is True

    def test_org_absent_is_not_covered(self) -> None:
        idx = build_org_date_index([("other corp", datetime(2026, 6, 18))])
        assert is_covered_within_window(idx, "acme", datetime(2026, 6, 20), window_days=60) is False


class TestVictimOrgDedupEnabled:
    def test_default_is_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.delenv("VICTIM_ORG_DEDUP", raising=False)

        # Act / Assert
        assert victim_org_dedup_enabled() is True

    def test_flag_0_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setenv("VICTIM_ORG_DEDUP", "0")

        # Act / Assert
        assert victim_org_dedup_enabled() is False

    def test_flag_false_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VICTIM_ORG_DEDUP", "false")
        assert victim_org_dedup_enabled() is False

    def test_flag_1_stays_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VICTIM_ORG_DEDUP", "1")
        assert victim_org_dedup_enabled() is True


class TestFindRecentVictimOrgDuplicate:
    def test_match_within_24h_returns_org_and_prior_article(
        self, repo: RunHistoryRepository
    ) -> None:
        # Arrange
        _seed_posted_article(repo, article_id="prev", victim_org="Acme Corp", hours_ago=2)

        # Act
        result = find_recent_victim_org_duplicate(repo, ["Acme Corp"], within_hours=24)

        # Assert
        assert result is not None
        org, prior = result
        assert org == "Acme Corp"
        assert prior.article_id == "prev"

    def test_outside_24h_window_is_not_a_duplicate(self, repo: RunHistoryRepository) -> None:
        """24h 超の続報は正当な情報として通す (窓を絞る設計の核心)。"""
        # Arrange: 30h 前の投稿 (24h 窓の外)
        _seed_posted_article(repo, article_id="prev", victim_org="Acme Corp", hours_ago=30)

        # Act
        result = find_recent_victim_org_duplicate(repo, ["Acme Corp"], within_hours=24)

        # Assert
        assert result is None

    def test_whitespace_and_case_variation_matches_exactly(
        self, repo: RunHistoryRepository
    ) -> None:
        """表記ゆれは lower/trim のみで吸収する。"""
        # Arrange
        _seed_posted_article(repo, article_id="prev", victim_org="  Acme Corp  ", hours_ago=1)

        # Act
        result = find_recent_victim_org_duplicate(repo, ["acme corp"], within_hours=24)

        # Assert
        assert result is not None

    def test_different_org_name_does_not_match(self, repo: RunHistoryRepository) -> None:
        """部分一致・fuzzy はしない — 誤爆で別組織の事案を潰さない。"""
        # Arrange
        _seed_posted_article(repo, article_id="prev", victim_org="Acme Corp", hours_ago=1)

        # Act
        result = find_recent_victim_org_duplicate(repo, ["Acme Corporation"], within_hours=24)

        # Assert
        assert result is None

    def test_excludes_self_article_id(self, repo: RunHistoryRepository) -> None:
        # Arrange: 自分自身の記事が既に posted 済みとして DB にある想定
        _seed_posted_article(repo, article_id="self-art", victim_org="Acme Corp", hours_ago=0.1)

        # Act
        result = find_recent_victim_org_duplicate(
            repo, ["Acme Corp"], within_hours=24, exclude_article_id="self-art"
        )

        # Assert
        assert result is None

    def test_empty_org_list_returns_none(self, repo: RunHistoryRepository) -> None:
        assert find_recent_victim_org_duplicate(repo, [], within_hours=24) is None

    def test_blank_org_entries_are_skipped(self, repo: RunHistoryRepository) -> None:
        result = find_recent_victim_org_duplicate(repo, ["   ", ""], within_hours=24)
        assert result is None

    def test_non_posted_status_is_not_a_duplicate(self, repo: RunHistoryRepository) -> None:
        """status='collected' (ransomware.live の非 JP 取込) は posted でないため対象外。"""
        # Arrange
        _seed_posted_article(
            repo, article_id="prev", victim_org="Acme Corp", hours_ago=1, status="collected"
        )

        # Act
        result = find_recent_victim_org_duplicate(repo, ["Acme Corp"], within_hours=24)

        # Assert
        assert result is None
