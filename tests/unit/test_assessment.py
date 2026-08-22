"""src.assessment.context: 横断 assessment 基盤のテスト。

段1 (出力中心→状態中心) で synthesis から抽出した builder 群が、bundle
(build_assessment_context) で個別 builder と同一結果を返すことを lock する。
nation_correlation の分離ロジック自体は test_synthesis 側でも検証済 (移設前後同一)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.assessment.context import (
    AssessmentContext,
    build_assessment_context,
    build_forecast_indicators,
    build_freshness,
    build_nation_correlation,
)
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


def _now() -> datetime:
    return datetime.now(UTC)


def _seed(repo: RunHistoryRepository) -> None:
    """帰属サイバー (actor.nation=cn) + 地政学 (involved_country=CN) を 1 件ずつ。"""
    now = _now()
    rid = repo.start_run(RunRecord(started_at=now, pipeline="t", dry_run=True))

    def art(aid: str, cat: str, **kw: object) -> ArticleRecord:
        return ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=aid,
            url=f"https://e/{aid}",
            category=cat,
            status="posted",
            created_at=now,
            published_at=now,
            **kw,  # type: ignore[arg-type]
        )

    # 攻撃者面は主題 (subject) 起点 (2026-08-20 反転) のため、mention だけでなく
    # subject_actor_ids/source も評価済みとして設定する。
    repo.add_article(
        art(
            "a1",
            "apt",
            socio_political_intent="prepositioning",
            subject_actor_ids="volt_typhoon",
            subject_actor_source="llm",
        )
    )
    repo.add_article_entities("a1", [("actor", "volt_typhoon")], when=now)
    repo.add_article(
        art("a2", "geopolitical", pmesii_i_cyber=True, socio_political_intent="coercion")
    )
    repo.add_article_entities("a2", [("involved_country", "CN")], when=now)


def test_build_assessment_context_bundles_individual_builders(tmp_path: Path) -> None:
    """bundle が個別 builder と同一結果を返す (= 状態中心の単一ソース化が無損失)。"""
    db = tmp_path / "asmt.db"
    repo = RunHistoryRepository(db_path=db)
    _seed(repo)

    ctx = build_assessment_context(
        nation_window_days=3650,
        freshness_lookback_hours=24 * 3650,
        db_path=db,
    )
    assert isinstance(ctx, AssessmentContext)

    # bundle の各フィールド == 個別 builder の戻り値
    assert ctx.nation_correlation == build_nation_correlation(window_days=3650, db_path=db)
    assert ctx.forecast_indicators == build_forecast_indicators(db_path=db)
    assert ctx.freshness == build_freshness(lookback_hours=24 * 3650, db_path=db)


def test_assessment_context_is_frozen() -> None:
    """状態オブジェクトは immutable (coding-style: イミュータブル優先)。"""
    import dataclasses

    ctx = AssessmentContext(nation_correlation=[], forecast_indicators=[], freshness={})
    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.nation_correlation = [{"x": 1}]  # type: ignore[misc]


def test_nation_correlation_present_after_seed(tmp_path: Path) -> None:
    """seed 後に CN が帰属/地政学の両レーンで現れる (builder が DB を読めている)。"""
    db = tmp_path / "asmt2.db"
    repo = RunHistoryRepository(db_path=db)
    _seed(repo)
    ctx = build_assessment_context(
        nation_window_days=3650, freshness_lookback_hours=24 * 3650, db_path=db
    )
    cn = next((n for n in ctx.nation_correlation if n["iso"] == "CN"), None)
    assert cn is not None
    assert cn["attributed_cyber"] == 1
    assert cn["cyber_mention"] == 1


def test_empty_db_returns_graceful_empties(tmp_path: Path) -> None:
    """記事ゼロでも例外なく空/0 を返す (障害時 legacy 挙動)。"""
    db = tmp_path / "empty.db"
    RunHistoryRepository(db_path=db)  # schema 初期化のみ
    ctx = build_assessment_context(nation_window_days=7, freshness_lookback_hours=168, db_path=db)
    assert ctx.nation_correlation == []
    assert ctx.forecast_indicators == []
    assert ctx.freshness["dated"] == 0
    assert ctx.freshness["retrospective_pct"] == 0


def test_forecast_indicators_exclude_daily_bursts_by_default(tmp_path: Path) -> None:
    """period_type 分離 (2026-08-22 独立レビュー P0)。

    日次バースト (period_type='daily'、当日件数) が週次消費者 (Spotlight) の
    構造予測ブロックへ流入すると、z 順で週次 FC2 を押し出し時間軸が混在する。
    既定 (weekly) では daily 行を返さないことを固定する。
    """
    from datetime import UTC, datetime

    from src.forecast.models import ForecastIndicatorRecord

    db = tmp_path / "fc.db"
    repo = RunHistoryRepository(db_path=db)
    now = datetime.now(UTC)
    repo.upsert_forecast_indicator(
        ForecastIndicatorRecord(
            period_type="weekly",
            period_start=now,
            scope="actor",
            target_value="volt_typhoon",
            direction="rising",
            z_score=2.4,
            baseline_avg=10.0,
            latest_count=38,
        )
    )
    repo.upsert_forecast_indicator(
        ForecastIndicatorRecord(
            period_type="daily",
            period_start=now,
            scope="actor",
            target_value="head_mare",
            direction="rising",
            z_score=6.8,  # 日次の z は週次より高く出やすい (押し出しの再現)
            baseline_avg=1.8,
            latest_count=8,
        )
    )

    rows = build_forecast_indicators(db_path=db)

    assert [r["target"] for r in rows] == ["volt_typhoon"]
    daily_rows = build_forecast_indicators(db_path=db, period_type="daily")
    assert [r["target"] for r in daily_rows] == ["head_mare"]
