"""Phase 6 (過去参照): time-machine 振り返りのテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.forecast.models import ForecastIndicatorRecord
from src.storage.run_history import (
    ArticleRecord,
    RunHistoryRepository,
    RunRecord,
    StatusSynthesisRecord,
)
from src.ui.services.retrospect import _week_window, build_retrospect


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "retro.db")


def test_week_window_shifts_back() -> None:
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)  # 水曜
    s0, e0, _ = _week_window(0, now)
    s1, e1, _ = _week_window(1, now)
    assert e0 - s0 == timedelta(days=7)
    assert s0 - s1 == timedelta(days=7)  # 1 週前は 7 日前


class TestBuildRetrospect:
    def test_assembles_week_snapshot(self, repo: RunHistoryRepository) -> None:
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        start, end, _ = _week_window(1, now)
        mid = start + timedelta(days=2)

        # その週の synthesis
        repo.upsert_status_synthesis(
            StatusSynthesisRecord(
                period_type="weekly",
                period_start=start,
                period_end=end,
                headline="先週の総括",
                weight_section="w",
                chain_section="c",
                cog_section="cog",
                spillover_section="s",
                pir_section="p",
                axes_evidence="{}",
            )
        )
        # その週の記事 + actor
        rid = repo.start_run(RunRecord(started_at=mid, pipeline="x", dry_run=False))
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id="r1",
                title="先週の重要記事",
                url="u",
                importance="high",
                status="posted",
                created_at=mid,
            )
        )
        repo.add_article_entities("r1", [("actor", "lazarus")], when=mid)
        # その週に立てた forecast 指標 (検証済 hit)
        iid = repo.upsert_forecast_indicator(
            ForecastIndicatorRecord(
                period_type="weekly",
                period_start=start,
                scope="actor",
                target_value="lazarus",
                direction="rising",
            )
        )
        repo.mark_forecast_indicator_verified(iid, hit=True, observed_count=3)

        snap = build_retrospect(repo, weeks_ago=1, now=now)
        assert snap["has_data"] is True
        assert snap["synthesis"]["headline"] == "先週の総括"
        assert any(a["article_id"] == "r1" for a in snap["top_articles"])
        assert any(a["value"] == "lazarus" for a in snap["top_actors"])
        outcome = snap["forecast_outcomes"][0]
        assert outcome["target_value"] == "lazarus"
        assert outcome["hit"] is True
        assert outcome["verified"] is True

    def test_empty_week_no_data(self, repo: RunHistoryRepository) -> None:
        snap = build_retrospect(repo, weeks_ago=5)
        assert snap["has_data"] is False
        assert snap["synthesis"] is None

    def test_excludes_other_weeks(self, repo: RunHistoryRepository) -> None:
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        this_start, _, _ = _week_window(0, now)
        # 今週の記事は「1 週前」snapshot に出ない
        rid = repo.start_run(
            RunRecord(started_at=this_start + timedelta(days=1), pipeline="x", dry_run=False)
        )
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id="thisweek",
                title="今週",
                url="u",
                importance="high",
                status="posted",
                created_at=this_start + timedelta(days=1),
            )
        )
        snap = build_retrospect(repo, weeks_ago=1, now=now)
        assert not any(a["article_id"] == "thisweek" for a in snap["top_articles"])


class TestWeeklyRecapPersistence:
    """段5: recap 本文の永続化 + Retrospect 連携。"""

    def test_record_and_get_in_window(self, repo: RunHistoryRepository) -> None:
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        start, end, _ = _week_window(1, now)
        mid = start + timedelta(days=2)
        repo.record_weekly_recap(
            run_id=None,
            period_label="2026-06-01 - 2026-06-07",
            recap_text="## 深掘り\n- Volt Typhoon ...",
            candidate_count=3,
            generated_at=mid,
        )
        got = repo.get_weekly_recap_in_window(start=start, end=end)
        assert got is not None
        assert got["candidate_count"] == 3
        assert "Volt Typhoon" in got["recap_text"]

    def test_out_of_window_returns_none(self, repo: RunHistoryRepository) -> None:
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        start, end, _ = _week_window(1, now)
        # window より後 (今週) に記録 → 1 週前 window では None
        repo.record_weekly_recap(
            run_id=None,
            period_label="now",
            recap_text="x",
            candidate_count=1,
            generated_at=end + timedelta(days=1),
        )
        assert repo.get_weekly_recap_in_window(start=start, end=end) is None

    def test_build_retrospect_includes_recap(self, repo: RunHistoryRepository) -> None:
        now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        start, _, _ = _week_window(1, now)
        repo.record_weekly_recap(
            run_id=None,
            period_label="wk",
            recap_text="今週の深掘り narrative",
            candidate_count=4,
            generated_at=start + timedelta(days=2),
        )
        snap = build_retrospect(repo, weeks_ago=1, now=now)
        assert snap["recap"] is not None
        assert snap["recap"]["recap_text"] == "今週の深掘り narrative"
        assert snap["has_data"] is True


class TestBuildBriefContext:
    """ブリーフ閲覧時の補足コンテキスト (時間軸統合 P2/P3)。"""

    def test_assembles_24h_actors_and_week_forecasts(self, repo: RunHistoryRepository) -> None:
        from src.ui.services.retrospect import build_brief_context

        until = datetime(2026, 6, 10, 21, 30, tzinfo=UTC)  # 水曜 06:30 JST 相当
        inside = until - timedelta(hours=3)
        outside = until - timedelta(hours=30)  # 24h 窓の外

        rid = repo.start_run(RunRecord(started_at=inside, pipeline="x", dry_run=False))
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id="c1",
                title="窓内記事",
                url="u",
                importance="high",
                status="posted",
                created_at=inside,
            )
        )
        repo.add_article_entities("c1", [("actor", "qilin")], when=inside)
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id="c2",
                title="窓外記事",
                url="u2",
                importance="high",
                status="posted",
                created_at=outside,
            )
        )
        repo.add_article_entities("c2", [("actor", "turla")], when=outside)

        # until が属する週の weekly 予測 (未検証 = 観測中)
        wk_start, _, _ = _week_window(0, until)
        repo.upsert_forecast_indicator(
            ForecastIndicatorRecord(
                period_type="weekly",
                period_start=wk_start,
                scope="actor",
                target_value="qilin",
                direction="rising",
            )
        )

        ctx = build_brief_context(repo, until=until)
        actor_values = [a["value"] for a in ctx["top_actors"]]
        assert "qilin" in actor_values
        assert "turla" not in actor_values  # 24h 窓の外は含めない
        fc = ctx["forecast_indicators"][0]
        assert fc["target_value"] == "qilin"
        assert fc["verified"] is False  # 観測中
        assert "JST" in ctx["window_label"]
