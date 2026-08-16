"""判定基準カードの field-stats 集計 (WP-B3) の unit test。

SQL は SQLite (dev) と PostgreSQL (production) の両方で動く必要がある。ここでは tmp
SQLite に runs / articles / article_entities を投入して**数値の正しさ**を固定し、方言差
(bare column GROUP BY・集約列の名前衝突) は実 PG での 1 回実行で確認する。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.pipeline.summary import SummaryOutput
from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository
from src.ui.services import rubric_field_stats
from src.ui.services.fill_rate_audit import METRICS
from src.ui.services.rubric_field_stats import (
    _build_scalar_query,
    build_field_stats,
    field_stats_cached,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    rubric_field_stats._STATS_CACHE.clear()
    yield
    rubric_field_stats._STATS_CACHE.clear()


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "field_stats.db")


def _add(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    hours_ago: float = 1.0,
    status: str = "posted",
    **fields: Any,
) -> datetime:
    """配信済み記事を 1 件足す (run は毎回新規 = 実データと同じ複数 run 構造)。"""
    created = datetime.now(UTC) - timedelta(hours=hours_ago)
    run_id = repo.start_run(RunRecord(started_at=created, pipeline="test", dry_run=False))
    payload: dict[str, Any] = {
        "run_id": run_id,
        "article_id": article_id,
        "title": f"title-{article_id}",
        "url": f"https://kuebiko.example/{article_id}",
        "feed_title": "feed",
        "status": status,
        "created_at": created,
    }
    payload.update(fields)
    repo.add_article(ArticleRecord(**payload))
    return created


def _field(stats: dict[str, Any], field_id: str) -> dict[str, Any]:
    return dict(stats["fields"][field_id])


class TestDistribution:
    def test_importance_distribution_counts_and_shares(self, repo: RunHistoryRepository) -> None:
        # Arrange: high 1 / medium 2 / low 1 の 4 件
        _add(repo, article_id="a1", importance="high", category="apt")
        _add(repo, article_id="a2", importance="medium", category="apt")
        _add(repo, article_id="a3", importance="medium", category="apt")
        _add(repo, article_id="a4", importance="low", category="apt")

        # Act
        stats = build_field_stats(7, db_path=repo.db_path)

        # Assert
        assert stats["denominator"]["count"] == 4
        dist = {b["value"]: b for b in _field(stats, "importance")["distribution"]}
        assert dist["medium"]["count"] == 2
        assert dist["medium"]["share"] == 0.5
        assert dist["high"]["count"] == 1
        assert dist["high"]["share"] == 0.25
        assert dist["high"]["vocab"] == "importance"
        assert dist["high"]["drilldown_query"] == "importance=high&since=168"

    def test_null_value_becomes_labeled_bucket_without_drilldown(
        self, repo: RunHistoryRepository
    ) -> None:
        # Arrange: category 未設定の記事 (生 enum を素通りさせない)
        _add(repo, article_id="a1", importance="low", category=None)

        # Act
        stats = build_field_stats(7, db_path=repo.db_path)

        # Assert
        empty = next(b for b in _field(stats, "category")["distribution"] if b["value"] == "")
        assert empty["label"] == "未設定"
        assert empty["vocab"] is None
        assert empty["drilldown_query"] is None


class TestAvailability:
    def test_columnless_fields_report_none_not_zero(self, repo: RunHistoryRepository) -> None:
        # Arrange
        _add(repo, article_id="a1", importance="high", category="apt")

        # Act
        stats = build_field_stats(7, db_path=repo.db_path)

        # Assert: 0% と「統計なし」を混同させない (数値を一切載せない)
        for field_id in ("analyst_note", "bluf"):
            stat = _field(stats, field_id)
            assert stat["availability"] == "none"
            assert stat["coverage"] is None
            assert stat["distribution"] == []
            assert stat["sub_metrics"] == []
            assert stat["average"] is None
            assert stat["source_note"]

    def test_all_summary_output_fields_are_present(self, repo: RunHistoryRepository) -> None:
        # Arrange
        _add(repo, article_id="a1", importance="high", category="apt")

        # Act
        stats = build_field_stats(7, db_path=repo.db_path)

        # Assert: カードからフィールドが消えない保証 (24 件すべて)
        assert set(stats["fields"]) == set(SummaryOutput.model_fields)
        assert len(stats["fields"]) == 24
        for field_id, stat in stats["fields"].items():
            assert stat["field_id"] == field_id
            assert stat["availability"] in {"full", "partial", "none"}


class TestZeroSafety:
    def test_empty_window_has_no_division_by_zero(self, repo: RunHistoryRepository) -> None:
        # Arrange: 記事 0 件 (新規運用 / 停止直後)

        # Act
        stats = build_field_stats(30, db_path=repo.db_path)

        # Assert
        assert stats["denominator"]["count"] == 0
        assert stats["window"]["days"] == 30
        for stat in stats["fields"].values():
            if stat["coverage"] is not None:
                assert stat["coverage"]["rate"] == 0.0
            for sub in stat["sub_metrics"]:
                assert sub["rate"] == 0.0
            for bucket in stat["distribution"]:
                assert bucket["share"] == 0.0

    def test_days_outside_endpoint_range_is_rejected(self, repo: RunHistoryRepository) -> None:
        # endpoint 側は Query(ge=1, le=90) で 422。service も同じ境界で fail fast する。
        for bad in (0, 999):
            with pytest.raises(ValueError, match="days"):
                build_field_stats(bad, db_path=repo.db_path)


class TestDenominator:
    def test_same_article_in_two_runs_counted_once(self, repo: RunHistoryRepository) -> None:
        # Arrange: 同一 article_id が 2 run 分 posted で残っている実データ構造
        _add(repo, article_id="dup", importance="high", category="apt", hours_ago=3)
        _add(repo, article_id="dup", importance="high", category="apt", hours_ago=1)
        _add(repo, article_id="other", importance="low", category="apt")

        # Act
        stats = build_field_stats(7, db_path=repo.db_path)

        # Assert
        assert stats["denominator"]["count"] == 2
        dist = {b["value"]: b["count"] for b in _field(stats, "importance")["distribution"]}
        assert dist["high"] == 1

    def test_non_posted_articles_are_excluded(self, repo: RunHistoryRepository) -> None:
        # Arrange: 取込のみ / 重複スキップは分母に含めない
        _add(repo, article_id="a1", importance="high", category="apt")
        _add(repo, article_id="a2", importance="high", category="apt", status="collected")
        _add(repo, article_id="a3", importance="high", category="apt", status="skipped_duplicate")

        # Act
        stats = build_field_stats(7, db_path=repo.db_path)

        # Assert
        assert stats["denominator"]["count"] == 1


class TestScopedCoverage:
    def test_coverage_conditions_come_from_fill_rate_audit(self) -> None:
        # 定義複製の検出: 充足条件は fill_rate_audit.METRICS が SSoT
        sql, _params, _aggs = _build_scalar_query("2026-08-01T00:00:00+00:00")
        for key in ("victim_country", "remediation", "victim_sector", "compromise_date"):
            metric = next(m for m in METRICS if m.key == key)
            assert metric.condition in sql

    def test_remediation_uses_vulnerability_scope(self, repo: RunHistoryRepository) -> None:
        # Arrange: vuln 2 件 (1 件のみ remediation あり) + apt 1 件 (分母外)
        _add(repo, article_id="v1", category="vulnerability", remediation="パッチ適用")
        _add(repo, article_id="v2", category="advisory")
        _add(repo, article_id="a1", category="apt")

        # Act
        stats = build_field_stats(7, db_path=repo.db_path)

        # Assert
        cov = _field(stats, "remediation")["coverage"]
        assert (cov["filled"], cov["total"], cov["rate"]) == (1, 2, 0.5)
        assert _field(stats, "remediation")["average"]["unit"] == "字"

    def test_summary_bands_and_average(self, repo: RunHistoryRepository) -> None:
        # Arrange: 50 字 / 300 字 / summary なし
        _add(repo, article_id="s1", category="apt", summary="あ" * 50)
        _add(repo, article_id="s2", category="apt", summary="い" * 300)
        _add(repo, article_id="s3", category="apt", summary=None)

        # Act
        stats = build_field_stats(7, db_path=repo.db_path)
        stat = _field(stats, "summary")

        # Assert
        bands = {b["value"]: b["count"] for b in stat["distribution"]}
        assert bands["0-99"] == 1
        assert bands["250-499"] == 1
        assert stat["coverage"]["filled"] == 2
        assert stat["coverage"]["total"] == 3
        assert stat["average"]["value"] == 175.0

    def test_pmesii_axes_sub_metrics_and_average(self, repo: RunHistoryRepository) -> None:
        # Arrange: 1 件は 2 軸、1 件は 0 軸
        _add(repo, article_id="p1", category="apt", pmesii_i_cyber=True, pmesii_p=True)
        _add(repo, article_id="p2", category="apt")

        # Act
        stats = build_field_stats(7, db_path=repo.db_path)
        stat = _field(stats, "pmesii_axes")

        # Assert
        subs = {s["key"]: s for s in stat["sub_metrics"]}
        assert len(subs) == 7 and "T" not in subs
        assert subs["I-cyber"]["filled"] == 1
        assert subs["I-cyber"]["vocab"] == "pmesii_axis"
        assert stat["coverage"]["filled"] == 1
        assert stat["average"]["value"] == 1.0


class TestEntityFields:
    def test_entity_coverage_average_and_top_values(self, repo: RunHistoryRepository) -> None:
        # Arrange: cyber 2 件に ttp、うち 1 件は 2 個。地政学 1 件は分母外
        for article_id in ("c1", "c2"):
            created = _add(repo, article_id=article_id, category="apt")
            repo.add_article_entities(article_id, [("ttp", "T1566")], when=created)
        repo.add_article_entities(
            "c1", [("ttp", "T1059")], when=datetime.now(UTC) - timedelta(hours=1)
        )
        _add(repo, article_id="g1", category="geopolitical")

        # Act
        stats = build_field_stats(7, db_path=repo.db_path)
        stat = _field(stats, "mitre_techniques")

        # Assert: 分母は cyber 系のみ (2 件)、平均は抽出のある記事あたり 3/2
        assert (stat["coverage"]["filled"], stat["coverage"]["total"]) == (2, 2)
        assert stat["average"]["value"] == 1.5
        top = {b["value"]: b["count"] for b in stat["distribution"]}
        assert top == {"T1566": 2}  # 1 件しか出ない T1059 は上位値に載せない

    def test_involved_countries_uses_geopolitical_scope(self, repo: RunHistoryRepository) -> None:
        # Arrange: 地政学 2 件に involved_country、cyber 1 件は分母外
        for article_id in ("g1", "g2"):
            created = _add(repo, article_id=article_id, category="geopolitical")
            repo.add_article_entities(article_id, [("involved_country", "CN")], when=created)
        _add(repo, article_id="c1", category="apt")

        # Act
        stats = build_field_stats(7, db_path=repo.db_path)
        stat = _field(stats, "involved_countries")

        # Assert
        assert (stat["coverage"]["filled"], stat["coverage"]["total"]) == (2, 2)
        assert stat["coverage"]["scope_label"] == "地政学・政策カテゴリの記事"
        assert stat["distribution"][0]["value"] == "CN"
        assert stat["distribution"][0]["vocab"] == "country"

    def test_iocs_count_all_ioc_types_once_per_article(self, repo: RunHistoryRepository) -> None:
        # Arrange: 1 記事に cve 2 件 + domain 1 件 (coverage は記事単位で 1)
        created = _add(repo, article_id="c1", category="vulnerability")
        repo.add_article_entities(
            "c1",
            [("cve", "CVE-2026-1111"), ("cve", "CVE-2026-2222"), ("ioc_domain", "bad.example")],
            when=created,
        )
        _add(repo, article_id="c2", category="vulnerability")

        # Act
        stats = build_field_stats(7, db_path=repo.db_path)
        stat = _field(stats, "iocs")

        # Assert
        assert (stat["coverage"]["filled"], stat["coverage"]["total"]) == (1, 2)
        assert stat["average"]["value"] == 3.0
        types = {b["value"]: b["count"] for b in stat["distribution"]}
        assert types == {"cve": 2, "ioc_domain": 1}


class TestWindowUpperBound:
    """窓の上限 (``until``)。過去の窓を切り出す呼び手 (切替前後の比較) のための追加引数。"""

    def test_articles_after_until_are_excluded(self, repo: RunHistoryRepository) -> None:
        # Arrange: until を 2 時間前に置き、1 時間前の記事を窓の外にする
        _add(repo, article_id="inside", importance="high", category="apt", hours_ago=5)
        _add(repo, article_id="outside", importance="low", category="apt", hours_ago=1)

        # Act
        stats = build_field_stats(
            7, db_path=repo.db_path, until=datetime.now(UTC) - timedelta(hours=2)
        )

        # Assert
        assert stats["denominator"]["count"] == 1
        dist = {b["value"]: b["count"] for b in _field(stats, "importance")["distribution"]}
        assert dist == {"high": 1}
        assert _field(stats, "title_ja")["coverage"]["total"] == 1

    def test_entity_fields_respect_the_upper_bound(self, repo: RunHistoryRepository) -> None:
        # Arrange: entity 側は EXISTS 内の articles 条件にも上限が要る (漏れると分母だけ減る)
        for article_id, hours_ago in (("c1", 5.0), ("c2", 1.0)):
            created = _add(repo, article_id=article_id, category="apt", hours_ago=hours_ago)
            repo.add_article_entities(article_id, [("ttp", "T1566")], when=created)

        # Act
        stats = build_field_stats(
            7, db_path=repo.db_path, until=datetime.now(UTC) - timedelta(hours=2)
        )

        # Assert: 窓内の 1 件だけが分子・分母の両方に入る
        cov = _field(stats, "mitre_techniques")["coverage"]
        assert (cov["filled"], cov["total"]) == (1, 1)

    def test_window_payload_reports_the_given_until(self, repo: RunHistoryRepository) -> None:
        # Arrange
        until = datetime(2026, 8, 16, 7, 1, 26, tzinfo=UTC)

        # Act
        stats = build_field_stats(7, db_path=repo.db_path, until=until)

        # Assert
        assert stats["window"]["until"] == "2026-08-16T07:01:26Z"
        assert stats["window"]["since"] == "2026-08-09T07:01:26Z"

    def test_omitted_until_produces_the_legacy_sql(self) -> None:
        # 既存 endpoint / cache の挙動を 1 文字も変えないことの検出器
        sql_default, params_default, _ = _build_scalar_query("2026-08-01T00:00:00+00:00")
        sql_bounded, params_bounded, _ = _build_scalar_query(
            "2026-08-01T00:00:00+00:00", "2026-08-08T00:00:00+00:00"
        )
        assert "created_at < ?" not in sql_default
        assert sql_bounded == sql_default + " AND a.created_at < ?"
        assert params_bounded == [*params_default, "2026-08-08T00:00:00+00:00"]


class TestCache:
    def test_second_call_within_ttl_does_not_query_db(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        _add(repo, article_id="a1", importance="high", category="apt")
        calls = {"n": 0}

        def fake_connect(db_path: Any = None, **kwargs: Any) -> Any:
            calls["n"] += 1
            return sqlite3.connect(repo.db_path)

        monkeypatch.setattr(rubric_field_stats, "connect", fake_connect)

        # Act
        first = field_stats_cached(7)
        second = field_stats_cached(7)

        # Assert
        assert calls["n"] == 1
        assert first["generated_at"] == second["generated_at"]

    def test_different_days_are_cached_separately(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        _add(repo, article_id="a1", importance="high", category="apt")
        calls = {"n": 0}

        def fake_connect(db_path: Any = None, **kwargs: Any) -> Any:
            calls["n"] += 1
            return sqlite3.connect(repo.db_path)

        monkeypatch.setattr(rubric_field_stats, "connect", fake_connect)

        # Act
        assert field_stats_cached(7)["window"]["days"] == 7
        assert field_stats_cached(30)["window"]["days"] == 30

        # Assert
        assert calls["n"] == 2
