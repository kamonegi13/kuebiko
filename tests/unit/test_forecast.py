"""Phase 4 (将来予測): analytics / storage / service のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.forecast import analytics
from src.forecast.models import ForecastIndicatorRecord
from src.forecast.service import build_forecast, snapshot_and_verify_indicators
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord

# ======================= analytics (pure) =======================


class TestBucketWeekly:
    def test_buckets_into_weeks_oldest_first(self) -> None:
        now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)  # 月曜
        times = [
            now - timedelta(days=1),  # 直近週
            now - timedelta(days=2),  # 直近週
            now - timedelta(days=8),  # 1 週前
            now - timedelta(days=20),  # 2-3 週前
        ]
        weekly = analytics.bucket_weekly(times, weeks=4, now=now)
        assert len(weekly) == 4
        assert weekly[-1] == 2  # 直近週に 2 件
        assert sum(weekly) == 4

    def test_ignores_future_and_out_of_window(self) -> None:
        now = datetime(2026, 6, 8, tzinfo=UTC)
        times = [now + timedelta(days=1), now - timedelta(days=400)]
        assert analytics.bucket_weekly(times, weeks=4, now=now) == [0, 0, 0, 0]


class TestBucketDaily:
    def test_buckets_into_days_oldest_first_with_today_last(self) -> None:
        now = datetime(2026, 6, 8, 15, 0, tzinfo=UTC)
        times = [
            now - timedelta(hours=2),  # 当日
            now - timedelta(hours=5),  # 当日
            now - timedelta(days=1),  # 前日
            now - timedelta(days=10),  # 窓内 (10 日前)
        ]
        daily = analytics.bucket_daily(times, days=28, now=now)
        assert len(daily) == 28
        assert daily[-1] == 2  # 当日に 2 件
        assert daily[-2] == 1  # 前日に 1 件
        assert sum(daily) == 4

    def test_ignores_future_and_out_of_window(self) -> None:
        now = datetime(2026, 6, 8, tzinfo=UTC)
        times = [now + timedelta(hours=1), now - timedelta(days=40)]
        assert analytics.bucket_daily(times, days=28, now=now) == [0] * 28

    def test_empty_times_returns_zero_series(self) -> None:
        now = datetime(2026, 6, 8, tzinfo=UTC)
        assert analytics.bucket_daily([], days=28, now=now) == [0] * 28

    def test_zero_days_returns_empty_series(self) -> None:
        now = datetime(2026, 6, 8, tzinfo=UTC)
        assert analytics.bucket_daily([now], days=0, now=now) == []


class TestTrendDirection:
    def test_increasing(self) -> None:
        d, slope = analytics.trend_direction([0, 1, 2, 4, 6])
        assert d == "increasing"
        assert slope > 0

    def test_decreasing(self) -> None:
        d, _ = analytics.trend_direction([6, 4, 2, 1, 0])
        assert d == "decreasing"

    def test_stable_flat(self) -> None:
        d, _ = analytics.trend_direction([3, 3, 3, 3])
        assert d == "stable"

    def test_empty_or_zero(self) -> None:
        assert analytics.trend_direction([])[0] == "stable"
        assert analytics.trend_direction([0, 0, 0])[0] == "stable"


class TestSpikeScore:
    def test_variance_aware_spike(self) -> None:
        # baseline が安定して低い (1,1,1,1) → 直近 8 は明確な spike
        z, is_spike = analytics.spike_score([1, 1, 1, 1, 8])
        assert is_spike
        assert z > 2.0

    def test_no_spike_within_variance(self) -> None:
        # 元々ばらつく系列 (0,8,0,8) で直近 6 は通常変動内 → spike にしない
        _, is_spike = analytics.spike_score([0, 8, 0, 8, 6])
        assert not is_spike

    def test_new_entity_spike(self) -> None:
        # baseline 全 0、直近に複数出現 → spike
        _, is_spike = analytics.spike_score([0, 0, 0, 0, 5])
        assert is_spike

    def test_below_min_count_not_spike(self) -> None:
        _, is_spike = analytics.spike_score([0, 0, 0, 0, 1])
        assert not is_spike


class TestPearson:
    def test_positive_correlation(self) -> None:
        assert analytics.pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)

    def test_zero_variance_returns_zero(self) -> None:
        assert analytics.pearson([1, 1, 1], [1, 2, 3]) == 0.0


# ======================= storage + service =======================


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "forecast.db")


def _seed_actor(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    actor: str,
    when: datetime,
    intent: str | None = None,
) -> None:
    rid = repo.start_run(RunRecord(started_at=when, pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=article_id,
            title="t",
            url=f"https://e/{article_id}",
            status="posted",
            socio_political_intent=intent,
            created_at=when,
        )
    )
    repo.add_article_entities(article_id, [("actor", actor)], when=when)


class TestEventTimes:
    def test_entity_event_times(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        _seed_actor(repo, article_id="a1", actor="lazarus", when=now)
        _seed_actor(repo, article_id="a2", actor="lazarus", when=now - timedelta(days=10))
        _seed_actor(repo, article_id="a3", actor="apt41", when=now)
        times = repo.entity_event_times("actor", since=now - timedelta(days=30))
        assert len(times["lazarus"]) == 2
        assert len(times["apt41"]) == 1

    def test_intent_event_times_excludes_unknown(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        _seed_actor(repo, article_id="a1", actor="x", when=now, intent="espionage")
        _seed_actor(repo, article_id="a2", actor="y", when=now, intent="unknown")
        times = repo.intent_event_times(since=now - timedelta(days=30))
        assert "espionage" in times
        assert "unknown" not in times


class TestForecastIndicatorCrud:
    def test_upsert_and_list(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        rec = ForecastIndicatorRecord(
            period_type="weekly",
            period_start=now,
            scope="actor",
            target_value="lazarus",
            direction="rising",
            z_score=3.2,
            baseline_avg=1.0,
            latest_count=5,
        )
        iid = repo.upsert_forecast_indicator(rec)
        assert iid >= 1
        items = repo.list_forecast_indicators(period_type="weekly")
        assert len(items) == 1
        assert items[0].target_value == "lazarus"
        assert items[0].hit is None

    def test_verify_and_hit_stats(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        rec = ForecastIndicatorRecord(
            period_type="weekly",
            period_start=now,
            scope="actor",
            target_value="lazarus",
            direction="rising",
            baseline_avg=1.0,
        )
        iid = repo.upsert_forecast_indicator(rec)
        repo.mark_forecast_indicator_verified(iid, hit=True, observed_count=4)
        verified, hits = repo.forecast_indicator_hit_stats(
            period_type="weekly", since=now - timedelta(days=1)
        )
        assert verified == 1
        assert hits == 1
        # 検証済みは only_open から外れる
        assert repo.list_forecast_indicators(period_type="weekly", only_open=True) == []

    def test_upsert_preserves_verification(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        rec = ForecastIndicatorRecord(
            period_type="weekly",
            period_start=now,
            scope="actor",
            target_value="lazarus",
            direction="rising",
            baseline_avg=1.0,
        )
        iid = repo.upsert_forecast_indicator(rec)
        repo.mark_forecast_indicator_verified(iid, hit=True, observed_count=4)
        # 再 snapshot (z_score 更新) しても検証結果は保持
        repo.upsert_forecast_indicator(rec.model_copy(update={"z_score": 9.9}))
        items = repo.list_forecast_indicators(period_type="weekly")
        assert items[0].hit is True
        assert items[0].z_score == pytest.approx(9.9)


class TestBuildForecast:
    def test_detects_new_actor_spike(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        # lazarus が直近週に集中 (baseline 0) → spike
        for i in range(4):
            _seed_actor(repo, article_id=f"a{i}", actor="lazarus", when=now)
        result = build_forecast(repo, weeks=8, now=now)
        assert result.has_data
        assert any(t.value == "lazarus" for t in result.actor_trends)
        assert any(s.value == "lazarus" for s in result.spike_alerts)

    def test_empty_db_no_data(self, repo: RunHistoryRepository) -> None:
        result = build_forecast(repo, weeks=8)
        assert result.has_data is False
        assert result.spike_alerts == []


class TestFC1PreviousSynthesis:
    def test_loads_prior_period_only(self, repo: RunHistoryRepository) -> None:
        from src.storage.run_history import StatusSynthesisRecord
        from src.synthesis.generator import _load_previous_synthesis

        now = datetime.now(UTC)
        prev_start = now - timedelta(weeks=1)
        for start, hl in [(prev_start, "前期総括"), (now, "今期総括")]:
            repo.upsert_status_synthesis(
                StatusSynthesisRecord(
                    period_type="weekly",
                    period_start=start,
                    period_end=start,
                    headline=hl,
                    weight_section="w",
                    chain_section="c",
                    cog_section=f"cog-{hl}",
                    spillover_section="s",
                    pir_section="p",
                    axes_evidence="{}",
                )
            )
        prev = _load_previous_synthesis(period_type="weekly", before=now, db_path=repo._db_path)
        assert prev is not None
        assert prev["headline"] == "前期総括"  # 今期は before 条件で除外される

    def test_none_when_no_prior(self, repo: RunHistoryRepository) -> None:
        from src.synthesis.generator import _load_previous_synthesis

        assert (
            _load_previous_synthesis(
                period_type="weekly", before=datetime.now(UTC), db_path=repo._db_path
            )
            is None
        )


class TestSnapshotAndVerify:
    def test_verifies_prior_then_snapshots(self, repo: RunHistoryRepository) -> None:
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)  # 水曜
        # 前期 indicator (2 週前を period_start) を未検証で投入
        prior_start = now - timedelta(days=14)
        repo.upsert_forecast_indicator(
            ForecastIndicatorRecord(
                period_type="weekly",
                period_start=prior_start,
                scope="actor",
                target_value="lazarus",
                direction="rising",
                baseline_avg=1.0,
            )
        )
        # 予測後 (prior_start 以降) に lazarus が継続活動 → hit になるべき
        for i in range(3):
            _seed_actor(repo, article_id=f"l{i}", actor="lazarus", when=now - timedelta(days=1))

        snapshotted, verified = snapshot_and_verify_indicators(repo, now=now)
        assert verified == 1
        # 前期 indicator が hit で確定
        all_ind = repo.list_forecast_indicators(period_type="weekly")
        prior = next(i for i in all_ind if i.period_start.date() == prior_start.date())
        assert prior.hit is True
        assert prior.observed_count == 3
        # 当期 spike が新規記録される (lazarus は直近週 spike)
        assert snapshotted >= 1


class TestForecastPirAnnotation:
    """④ forecast の PIR 連携 (matched_pir_ids + PIR 該当 spike 優先)。"""

    def test_match_pirs_for_value_bidirectional(self) -> None:
        from src.forecast.service import _match_pirs_for_value

        names = [("pir_dprk", "lazarus"), ("pir_cn", "volt typhoon")]
        # PIR "lazarus" ⊆ value "Lazarus Group"
        assert _match_pirs_for_value("Lazarus Group", names) == ["pir_dprk"]
        # 完全一致
        assert _match_pirs_for_value("Volt Typhoon", names) == ["pir_cn"]
        # 部分語誤爆なし ("gru" は "Grumman" の語境界に無い)
        assert _match_pirs_for_value("Grumman", [("p", "gru")]) == []
        assert _match_pirs_for_value("unrelated", names) == []

    def test_annotate_trends_is_immutable(self) -> None:
        from src.forecast.models import EntityTrend
        from src.forecast.service import _annotate_trends_with_pir

        t = EntityTrend(scope="actor", value="Lazarus Group", weekly=[1], total=1)
        out = _annotate_trends_with_pir([t], [("pir_dprk", "lazarus")])
        assert out[0].matched_pir_ids == ["pir_dprk"]
        assert t.matched_pir_ids == []  # 元 trend は不変 (immutable copy)

    def test_build_forecast_annotates_and_prioritizes(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.forecast.service as svc

        now = datetime.now(UTC)
        for i in range(4):
            _seed_actor(repo, article_id=f"l{i}", actor="lazarus", when=now)
        for i in range(6):
            _seed_actor(repo, article_id=f"g{i}", actor="globex", when=now)
        monkeypatch.setattr(svc, "_load_enabled_pir_actor_names", lambda: [("pir_dprk", "lazarus")])
        result = build_forecast(repo, weeks=8, now=now)

        laz = next(t for t in result.actor_trends if t.value == "lazarus")
        assert laz.matched_pir_ids == ["pir_dprk"]
        glob = next(t for t in result.actor_trends if t.value == "globex")
        assert glob.matched_pir_ids == []

        # PIR 該当 spike は非該当より前に並ぶ
        pir_idx = [i for i, s in enumerate(result.spike_alerts) if s.matched_pir_ids]
        non_idx = [i for i, s in enumerate(result.spike_alerts) if not s.matched_pir_ids]
        if pir_idx and non_idx:
            assert min(pir_idx) < min(non_idx)


class TestIndicatorVerdictV2:
    """FC2 v2 較正判定 (Poisson 2σ・週次正規化、監査 2026-07-16 設計 §4b)。"""

    @staticmethod
    def _verdict(baseline: float, observed: int, weeks: float = 1.0) -> str:
        from datetime import UTC, datetime, timedelta

        from src.forecast.service import indicator_verdict_v2

        start = datetime(2026, 7, 6, tzinfo=UTC)
        return indicator_verdict_v2(
            baseline_avg=baseline,
            observed_count=observed,
            period_start=start,
            verified_at=start + timedelta(days=7 * weeks),
        )

    def test_baseline_level_activity_is_partial_not_hit(self) -> None:
        # v1 の準トートロジー (観測=baseline で hit) が v2 では partial になる
        assert self._verdict(baseline=10.0, observed=10) == "partial"

    def test_significant_surge_is_hit(self) -> None:
        # baseline 10 の 2σ 閾値 ≈ 16.3 → 17 件は有意な増勢
        assert self._verdict(baseline=10.0, observed=17) == "hit"

    def test_collapse_below_band_is_miss(self) -> None:
        # baseline 10 − 2σ ≈ 3.7 → 2 件は下振れ (v1 では単に not-hit で不可視だった)
        assert self._verdict(baseline=10.0, observed=2) == "miss"

    def test_zero_observation_is_miss(self) -> None:
        assert self._verdict(baseline=0.5, observed=0) == "miss"

    def test_low_baseline_needs_floor(self) -> None:
        # baseline 0.25 でも週 1 件で hit にしない (低頻度フロア = 2 件/週)
        assert self._verdict(baseline=0.25, observed=1) == "partial"
        assert self._verdict(baseline=0.25, observed=2) == "hit"

    def test_multi_week_window_is_normalized(self) -> None:
        # 2 週窓で 20 件 = 週次 10 件 → baseline 10 なら partial (累積インフレを除去)
        assert self._verdict(baseline=10.0, observed=20, weeks=2.0) == "partial"
