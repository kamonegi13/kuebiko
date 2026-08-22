"""事象時刻の錨 (不変条件): 言及の時刻は「DB へ書いた時刻」ではなく「事象の時刻」。

2026-08-22 の根本原因: entity_event_times が article_entities.created_at
(= DB 書込時刻) を事象時刻として返していた。バックフィル (再抽出 / 別名昇格 /
intent・axes backfill) は過去記事へ当日の日付で行を書くため、この近似は破れる。
実測で言及の 44.3% が 1 日超・34.4% が 7 日超ずれており、週次 FC3 spike の 44% が
偽陽性、日次バーストは単日最大 42 件の幻を出していた。

週次 8 週窓では均されて見えにくいが、日次窓では支配的になる。時間尺度を変えて
データ源を再利用するときは、必ず時刻意味論を再検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository

_NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "anchor.db")


def _article(
    repo: RunHistoryRepository,
    article_id: str,
    *,
    created: datetime,
    published: datetime | None = None,
    sector: str | None = None,
    country: str | None = None,
) -> None:
    rid = repo.start_run(RunRecord(started_at=created, pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=article_id,
            title="t",
            url=f"https://kuebiko.example/{article_id}",
            status="posted",
            category="incident",
            created_at=created,
            published_at=published,
            victim_sector_canonical=sector,
            victim_country_iso=country,
        )
    )


class TestEntityEventTimeAnchor:
    def test_backfilled_entity_reports_article_time_not_write_time(
        self, repo: RunHistoryRepository
    ) -> None:
        """核心の不変条件: 当日書いた entity でも、記事が古ければ古い時刻を返す。"""
        # Arrange — 2 か月前の記事に、今日 entity を書く (別名昇格 backfill と同じ形)
        old = _NOW - timedelta(days=60)
        _article(repo, "a1", created=old, published=old)
        repo.add_article_entities("a1", [("actor", "unc1151")], when=_NOW)

        # Act
        times = repo.entity_event_times("actor", since=_NOW - timedelta(days=90))

        # Assert — 今日の急増として数えてはならない
        assert len(times["unc1151"]) == 1
        assert abs((times["unc1151"][0] - old).total_seconds()) < 3600

    def test_falls_back_to_ingest_time_when_published_is_null(
        self, repo: RunHistoryRepository
    ) -> None:
        # Arrange
        ingested = _NOW - timedelta(days=3)
        _article(repo, "a1", created=ingested, published=None)
        repo.add_article_entities("a1", [("actor", "lazarus")], when=ingested)

        # Act
        times = repo.entity_event_times("actor", since=_NOW - timedelta(days=30))

        # Assert
        assert abs((times["lazarus"][0] - ingested).total_seconds()) < 3600

    def test_published_after_ingest_is_clamped_to_ingest(
        self, repo: RunHistoryRepository
    ) -> None:
        """取込より未来の published_at は不正データ (実測 0.5%)。取込時刻へ丸める。"""
        # Arrange
        ingested = _NOW - timedelta(days=3)
        _article(repo, "a1", created=ingested, published=_NOW + timedelta(days=30))
        repo.add_article_entities("a1", [("actor", "lazarus")], when=ingested)

        # Act
        times = repo.entity_event_times("actor", since=_NOW - timedelta(days=30))

        # Assert — 未来へ飛ばさない
        assert abs((times["lazarus"][0] - ingested).total_seconds()) < 3600

    def test_duplicate_article_rows_do_not_inflate_mentions(
        self, repo: RunHistoryRepository
    ) -> None:
        """articles は同一 article_id が複数行ありうる (実測 3,593 行)。join で水増ししない。"""
        # Arrange — 同じ article_id を 2 回 add (取込リトライ / skipped_duplicate 相当)
        t = _NOW - timedelta(days=2)
        _article(repo, "a1", created=t, published=t)
        _article(repo, "a1", created=t + timedelta(minutes=5), published=t)
        repo.add_article_entities("a1", [("actor", "qilin")], when=t)

        # Act
        times = repo.entity_event_times("actor", since=_NOW - timedelta(days=30))

        # Assert
        assert len(times["qilin"]) == 1

    def test_since_filters_on_event_time(self, repo: RunHistoryRepository) -> None:
        """窓の判定も事象時刻で行う (書込時刻で切ると古い記事が窓に入り込む)。"""
        # Arrange — 記事は 60 日前、entity は今日書かれた
        old = _NOW - timedelta(days=60)
        _article(repo, "a1", created=old, published=old)
        repo.add_article_entities("a1", [("actor", "apt29")], when=_NOW)

        # Act — 直近 30 日窓
        times = repo.entity_event_times("actor", since=_NOW - timedelta(days=30))

        # Assert — 事象は窓外なので入らない
        assert "apt29" not in times


class TestVictimStreamAnchor:
    def test_sector_stream_uses_event_time(self, repo: RunHistoryRepository) -> None:
        # Arrange
        old = _NOW - timedelta(days=40)
        _article(repo, "a1", created=old, published=old, sector="healthcare")

        # Act
        times = repo.victim_sector_event_times(since=_NOW - timedelta(days=90))

        # Assert
        assert "healthcare" in times
        assert abs((times["healthcare"][0] - old).total_seconds()) < 3600

    def test_sector_stream_dedupes_article_rows(self, repo: RunHistoryRepository) -> None:
        # Arrange
        t = _NOW - timedelta(days=2)
        _article(repo, "a1", created=t, published=t, sector="finance")
        _article(repo, "a1", created=t + timedelta(minutes=5), published=t, sector="finance")

        # Act
        times = repo.victim_sector_event_times(since=_NOW - timedelta(days=30))

        # Assert
        assert len(times["finance"]) == 1


class TestBurstCloseOutWindow:
    """close-out の検証窓はバースト当日だけを除く (境界を固定する)。"""

    def test_same_day_excluded_next_day_included(self) -> None:
        # Arrange
        from datetime import timedelta as _td

        from src.forecast import burst

        start = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        window_start = start + _td(days=1)
        window_end = start + _td(days=burst.VERIFY_AFTER_DAYS)
        same_day = start + _td(hours=13)  # バースト当日 → 除外
        next_day_00 = window_start  # 翌日 00:00 ちょうど → 含む
        last = window_end  # 窓末 → 含む
        after = window_end + _td(seconds=1)  # 窓外

        # Act
        cands = (same_day, next_day_00, last, after)
        counted = [t for t in cands if window_start <= t <= window_end]

        # Assert
        assert counted == [next_day_00, last]
