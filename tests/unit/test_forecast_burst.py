"""I&W 日次バースト検出 (src/forecast/burst.py) のテスト。

既存 forecast (週次 FC2〜FC5) の日次拡張。純粋ロジック (候補抽出/整形) と
storage 結線 (抑制/persist/close-out/新規 repo メソッド) を分けてテストする。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.forecast import burst
from src.forecast.models import ForecastIndicatorRecord
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord

# ======================= 純粋ロジック (repo 不要) =======================


class TestFindSpikeCandidates:
    def test_baseline_calm_then_today_burst_is_a_candidate(self) -> None:
        now = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
        # baseline (直近 27 日) はゼロ、当日に 5 件集中 → spike
        times = [now - timedelta(minutes=1)] * 5
        streams = {"actor": {"lazarus": times}, "sector": {}, "country": {}}
        candidates = burst._find_spike_candidates(streams, now=now)  # noqa: SLF001
        assert len(candidates) == 1
        c = candidates[0]
        assert (c.scope, c.value, c.today_count) == ("actor", "lazarus", 5)
        assert c.baseline_daily_avg == pytest.approx(0.0)
        assert c.label == "lazarus"  # actor は値をそのまま label に

    def test_below_min_count_is_not_a_candidate(self) -> None:
        now = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
        times = [now - timedelta(minutes=1)] * 2  # DAILY_SPIKE_MIN_COUNT=3 未満
        streams = {"actor": {"lazarus": times}, "sector": {}, "country": {}}
        assert burst._find_spike_candidates(streams, now=now) == []  # noqa: SLF001

    def test_noisy_but_within_baseline_variance_is_not_a_candidate(self) -> None:
        now = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
        # baseline 自体が高頻度にばらつく系列 + 通常範囲内の当日 → spike にしない
        times: list[datetime] = []
        for day_offset, n in enumerate([8, 0, 8, 0, 8, 0]):
            for _ in range(n):
                times.append(now - timedelta(days=day_offset + 1))
        times.extend([now - timedelta(minutes=1)] * 6)  # 当日 6 件 (変動内)
        streams = {"actor": {"noisy": times}, "sector": {}, "country": {}}
        assert burst._find_spike_candidates(streams, now=now) == []  # noqa: SLF001


class TestFormatBurstCompact:
    def test_empty_list_returns_empty_string(self) -> None:
        assert burst.format_burst_compact([]) == ""

    def test_includes_header_scope_prefix_and_pir_flag(self) -> None:
        event = burst.BurstEvent(
            scope="actor",
            value="lazarus",
            label="Lazarus",
            today_count=5,
            baseline_daily_avg=1.0,
            z_score=3.0,
            matched_pir_ids=("pir_dprk",),
        )
        text = burst.format_burst_compact([event])
        assert text.startswith("📈 急増検知 (I&W)")
        assert "アクター" in text
        assert "Lazarus" in text
        assert "⚑PIR" in text
        assert "本日 5 件" in text

    def test_non_actor_scope_never_shows_pir_flag(self) -> None:
        event = burst.BurstEvent(
            scope="sector",
            value="energy",
            label="エネルギー",
            today_count=4,
            baseline_daily_avg=0.5,
            z_score=2.9,
            matched_pir_ids=("pir_x",),  # sector には本来つかないが防御的に確認
        )
        assert "⚑PIR" not in burst.format_burst_compact([event])

    def test_caps_at_five_entries(self) -> None:
        events = [
            burst.BurstEvent(
                scope="sector",
                value=f"s{i}",
                label=f"s{i}",
                today_count=3,
                baseline_daily_avg=0.0,
                z_score=3.0,
            )
            for i in range(8)
        ]
        text = burst.format_burst_compact(events)
        lines = text.splitlines()
        assert len(lines) == 1 + 5  # header + 上位 5 件


# ======================= storage 結線 =======================


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "burst.db")


def _seed_actor(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    actor: str,
    when: datetime,
) -> None:
    rid = repo.start_run(RunRecord(started_at=when, pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=article_id,
            title="t",
            url=f"https://e/{article_id}",
            status="posted",
            created_at=when,
        )
    )
    repo.add_article_entities(article_id, [("actor", actor)], when=when)


def _seed_article(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    when: datetime,
    status: str = "posted",
    category: str = "breach",
    sector: str | None = None,
    country: str | None = None,
) -> None:
    rid = repo.start_run(RunRecord(started_at=when, pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=article_id,
            title="t",
            url=f"https://e/{article_id}",
            status=status,  # type: ignore[arg-type]
            category=category,
            victim_sector_canonical=sector,
            victim_country_iso=country,
            created_at=when,
        )
    )


class TestVictimSectorEventTimes:
    def test_only_posted_and_cyber_attack_category_included(
        self, repo: RunHistoryRepository
    ) -> None:
        now = datetime.now(UTC)
        _seed_article(repo, article_id="a1", when=now, status="posted", sector="energy")
        _seed_article(repo, article_id="a2", when=now, status="collected", sector="energy")
        _seed_article(
            repo,
            article_id="a3",
            when=now,
            status="posted",
            category="geopolitical",
            sector="energy",
        )
        times = repo.victim_sector_event_times(since=now - timedelta(days=1))
        assert list(times.keys()) == ["energy"]
        assert len(times["energy"]) == 1

    def test_excludes_uncategorized_and_multi_sector_and_null(
        self, repo: RunHistoryRepository
    ) -> None:
        now = datetime.now(UTC)
        _seed_article(repo, article_id="a1", when=now, sector="uncategorized")
        _seed_article(repo, article_id="a2", when=now, sector="multi_sector")
        _seed_article(repo, article_id="a3", when=now, sector=None)
        _seed_article(repo, article_id="a4", when=now, sector="energy")
        times = repo.victim_sector_event_times(since=now - timedelta(days=1))
        assert list(times.keys()) == ["energy"]


class TestVictimCountryEventTimes:
    def test_only_posted_and_cyber_attack_category_with_resolved_country(
        self, repo: RunHistoryRepository
    ) -> None:
        now = datetime.now(UTC)
        _seed_article(repo, article_id="a1", when=now, status="posted", country="jp")
        _seed_article(
            repo, article_id="a2", when=now, status="posted", category="geopolitical", country="jp"
        )
        _seed_article(repo, article_id="a3", when=now, status="posted", country=None)
        times = repo.victim_country_event_times(since=now - timedelta(days=1))
        assert list(times.keys()) == ["JP"]
        assert len(times["JP"]) == 1


class TestSuppression:
    def test_open_daily_indicator_within_window_suppresses_refiring(
        self, repo: RunHistoryRepository
    ) -> None:
        now = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
        repo.upsert_forecast_indicator(
            ForecastIndicatorRecord(
                period_type="daily",
                period_start=now - timedelta(days=3),
                scope="actor",
                target_value="lazarus",
                direction="rising",
                baseline_avg=1.0,
            )
        )
        for i in range(5):
            _seed_actor(repo, article_id=f"a{i}", actor="lazarus", when=now)

        events = burst.detect_daily_bursts(repo, now=now, persist=True)

        assert events == []  # 既に監視中 (open, 7 日以内) → 再起票しない

    def test_open_indicator_outside_window_does_not_suppress(
        self, repo: RunHistoryRepository
    ) -> None:
        now = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
        repo.upsert_forecast_indicator(
            ForecastIndicatorRecord(
                period_type="daily",
                period_start=now - timedelta(days=20),  # SUPPRESSION_DAYS(7) より外
                scope="actor",
                target_value="lazarus",
                direction="rising",
                baseline_avg=1.0,
            )
        )
        for i in range(5):
            _seed_actor(repo, article_id=f"a{i}", actor="lazarus", when=now)

        events = burst.detect_daily_bursts(repo, now=now, persist=True)

        assert len(events) == 1
        assert events[0].value == "lazarus"


class TestPersist:
    def test_persists_daily_indicator_with_week_equivalent_baseline(
        self, repo: RunHistoryRepository
    ) -> None:
        now = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
        event = burst.BurstEvent(
            scope="actor",
            value="lazarus",
            label="lazarus",
            today_count=9,
            baseline_daily_avg=2.0,
            z_score=4.0,
        )
        burst._persist_new_indicators(repo, [event], now=now)  # noqa: SLF001

        rows = repo.list_forecast_indicators(period_type="daily")
        assert len(rows) == 1
        row = rows[0]
        assert row.period_type == "daily"
        assert row.scope == "actor"
        assert row.direction == "rising"
        assert row.target_value == "lazarus"
        assert row.latest_count == 9
        assert row.baseline_avg == pytest.approx(14.0)  # 2.0 件/日 × 7 = 週換算
        assert row.period_start == now.astimezone(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    def test_end_to_end_detect_and_persist_new_actor_burst(
        self, repo: RunHistoryRepository
    ) -> None:
        now = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
        for i in range(5):
            _seed_actor(repo, article_id=f"a{i}", actor="lazarus", when=now)

        events = burst.detect_daily_bursts(repo, now=now, persist=True)

        assert len(events) == 1
        assert events[0].scope == "actor"
        assert events[0].value == "lazarus"
        rows = repo.list_forecast_indicators(period_type="daily")
        assert len(rows) == 1
        assert rows[0].target_value == "lazarus"


class TestCloseOut:
    def test_closes_stale_daily_indicator_without_touching_weekly(
        self, repo: RunHistoryRepository
    ) -> None:
        now = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
        stale_start = now - timedelta(days=8)  # VERIFY_AFTER_DAYS(7) を超過
        stale_id = repo.upsert_forecast_indicator(
            ForecastIndicatorRecord(
                period_type="daily",
                period_start=stale_start,
                scope="actor",
                target_value="lazarus",
                direction="rising",
                baseline_avg=7.0,  # 週換算 (日次 1.0 件/日相当)
            )
        )
        weekly_id = repo.upsert_forecast_indicator(
            ForecastIndicatorRecord(
                period_type="weekly",
                period_start=now - timedelta(weeks=3),
                scope="actor",
                target_value="lazarus",
                direction="rising",
                baseline_avg=1.0,
            )
        )
        # 観測窓 (stale_start, stale_start+7d] に 2 件 → baseline 7.0 未満 (miss)
        for i, day_offset in enumerate([1, 3]):
            _seed_actor(
                repo,
                article_id=f"obs{i}",
                actor="lazarus",
                when=stale_start + timedelta(days=day_offset),
            )

        burst.detect_daily_bursts(repo, now=now, persist=True)

        daily_rows = {i.id: i for i in repo.list_forecast_indicators(period_type="daily")}
        stale = daily_rows[stale_id]
        assert stale.verified_at is not None
        assert stale.observed_count == 2
        assert stale.hit is False

        weekly_rows = {i.id: i for i in repo.list_forecast_indicators(period_type="weekly")}
        weekly = weekly_rows[weekly_id]
        assert weekly.verified_at is None  # weekly は一切触られない
