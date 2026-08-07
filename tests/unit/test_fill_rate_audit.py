"""fill_rate_audit (供給網ヘルス監査、有機的結合監査 R1 の恒久対処) の unit test。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.services.fill_rate_audit import (
    METRICS,
    SENTINEL_KEYS,
    WeekCell,
    bucket_weekly,
    build_audit_report,
    build_drift_lines,
    build_feed_body_health_lines,
    build_heartbeat_fill_line,
    collect_weekly_cells,
    detect_feed_body_health_collapses,
    detect_fill_collapse,
    detect_fill_drift,
    fetch_daily_rows,
)


class TestBucketWeekly:
    def test_groups_days_into_iso_weeks(self) -> None:
        # Arrange: 月曜 7/6 の週と 7/13 の週
        rows = [
            ("2026-07-06", 10, 5),
            ("2026-07-08", 10, 3),
            ("2026-07-13", 20, 2),
        ]

        # Act
        cells = bucket_weekly("m", rows)

        # Assert
        assert [(c.week_start, c.n, c.filled) for c in cells] == [
            (date(2026, 7, 6), 20, 8),
            (date(2026, 7, 13), 20, 2),
        ]

    def test_skips_malformed_days(self) -> None:
        cells = bucket_weekly("m", [("garbage", 5, 1), ("2026-07-07", 5, 2)])

        assert len(cells) == 1
        assert cells[0].n == 5


def _cells(key: str, pcts: list[tuple[str, int, int]]) -> list[WeekCell]:
    return [
        WeekCell(metric_key=key, week_start=date.fromisoformat(d), n=n, filled=f)
        for d, n, f in pcts
    ]


class TestDetectFillCollapse:
    def test_warns_on_collapse(self) -> None:
        # Arrange: 過去 4 週 ~50% → 直近週 5% (intent 型の沈黙崩壊)
        cells = _cells(
            "intent",
            [
                ("2026-06-08", 100, 50),
                ("2026-06-15", 100, 52),
                ("2026-06-22", 100, 48),
                ("2026-06-29", 100, 50),
                ("2026-07-06", 100, 5),
            ],
        )

        # Act
        warn = detect_fill_collapse(cells, eval_week=date(2026, 7, 6), label="intent")

        # Assert
        assert warn is not None
        assert warn.current_pct == 5.0
        assert warn.baseline_pct == 50.0

    def test_no_warn_when_stable(self) -> None:
        cells = _cells("m", [("2026-06-29", 100, 50), ("2026-07-06", 100, 40)])

        assert detect_fill_collapse(cells, eval_week=date(2026, 7, 6), label="m") is None

    def test_mutes_low_baseline_metric(self) -> None:
        # compromise_date 型 (恒常 0-2%) は騒がせない
        cells = _cells(
            "m",
            [
                ("2026-06-22", 100, 2),
                ("2026-06-29", 100, 1),
                ("2026-07-06", 100, 0),
            ],
        )

        assert detect_fill_collapse(cells, eval_week=date(2026, 7, 6), label="m") is None

    def test_mutes_small_sample_week(self) -> None:
        cells = _cells("m", [("2026-06-29", 100, 50), ("2026-07-06", 10, 0)])

        assert detect_fill_collapse(cells, eval_week=date(2026, 7, 6), label="m") is None

    def test_none_when_eval_week_missing(self) -> None:
        cells = _cells("m", [("2026-06-29", 100, 50)])

        assert detect_fill_collapse(cells, eval_week=date(2026, 7, 6), label="m") is None


class TestMetricsRegistration:
    """監査 2026-08-01: METRICS 未登録の列が沈黙断線しても無警告だった穴の閉鎖。"""

    def test_audit_2026_08_01_columns_are_registered(self) -> None:
        keys = {m.key for m in METRICS}
        assert {"article_type", "victim_country", "is_ransomware"} <= keys


class TestDetectFillDrift:
    """緩慢劣化 (トレンド) 検知 — collapse 比 0.5 では不可視の単調ドリフトを捉える。

    実例: intent cyber 系が 6 月 87-88% → 7 月末 68% (-20pt) と 6 週かけて劣化したが、
    collapse は 44% 未満まで沈黙するため検知できなかった (監査 2026-08-01)。
    """

    @staticmethod
    def _drift_cells(recent_pcts: list[int]) -> list[WeekCell]:
        """6 月 4 週 ≈87% の基準 + 直近 3 週 (7/6, 7/13, 7/20) を組む。"""
        base = [
            ("2026-06-01", 100, 88),
            ("2026-06-08", 100, 87),
            ("2026-06-15", 100, 88),
            ("2026-06-22", 100, 86),
        ]
        recent = [(f"2026-07-{6 + 7 * i:02d}", 100, pct) for i, pct in enumerate(recent_pcts)]
        return _cells("intent", base + recent)

    def test_warns_on_sustained_drift(self) -> None:
        # Arrange: 基準 ~87% → 直近 3 週 72/69/68 (毎週 -15pt 超の持続低下)
        cells = self._drift_cells([72, 69, 68])

        # Act
        warn = detect_fill_drift(cells, eval_week=date(2026, 7, 20), label="intent")

        # Assert
        assert warn is not None
        assert warn.current_pct == 68.0
        assert warn.baseline_pct == 87.5

    def test_no_warn_on_single_week_dip(self) -> None:
        # 直近週だけ落ちて前 2 週は基準圏 → 一時的な谷 (ノイズ) は騒がせない
        cells = self._drift_cells([86, 85, 68])

        assert detect_fill_drift(cells, eval_week=date(2026, 7, 20), label="intent") is None

    def test_no_warn_on_small_drop(self) -> None:
        # -15pt 未満の低下は正常変動として扱う
        cells = self._drift_cells([80, 78, 79])

        assert detect_fill_drift(cells, eval_week=date(2026, 7, 20), label="intent") is None

    def test_mutes_low_baseline_metric(self) -> None:
        # 元々供給の無い列 (compromise_date 型) は対象外
        base = [(f"2026-06-{d:02d}", 100, 8) for d in (1, 8, 15, 22)]
        recent = [("2026-07-06", 100, 0), ("2026-07-13", 100, 0), ("2026-07-20", 100, 0)]
        cells = _cells("m", base + recent)

        assert detect_fill_drift(cells, eval_week=date(2026, 7, 20), label="m") is None

    def test_none_when_recent_weeks_incomplete(self) -> None:
        # 直近 3 週が揃っていなければ判定しない
        cells = _cells(
            "m",
            [("2026-06-08", 100, 88), ("2026-06-15", 100, 87), ("2026-07-20", 100, 60)],
        )

        assert detect_fill_drift(cells, eval_week=date(2026, 7, 20), label="m") is None

    def test_build_drift_lines(self) -> None:
        cells = self._drift_cells([72, 69, 68])
        warn = detect_fill_drift(cells, eval_week=date(2026, 7, 20), label="intent")
        assert warn is not None

        lines = build_drift_lines([warn])

        assert lines[0].startswith("fill 緩慢劣化")
        assert any("intent" in ln and "68" in ln for ln in lines[1:])
        assert build_drift_lines([]) == []


class TestBuildAuditReport:
    def test_ok_report_is_low(self) -> None:
        title, body, importance = build_audit_report(
            [], eval_week=date(2026, 7, 6), metrics_checked=16
        )

        assert "OK" in body
        assert importance == "low"
        assert "fill-rate" in title

    def test_warn_report_lists_metrics(self) -> None:
        from src.ui.services.fill_rate_audit import FillWarn

        warns = [
            FillWarn(
                metric_key="intent",
                label="intent",
                baseline_pct=50.0,
                current_pct=5.0,
                current_n=120,
            )
        ]

        _, body, importance = build_audit_report(
            warns, eval_week=date(2026, 7, 6), metrics_checked=16
        )

        assert "intent: 50% → 5%" in body
        assert importance == "medium"


class TestSqlIntegration:
    """SQLite dev スキーマで SQL の互換性を検証する (production は PG)。"""

    @pytest.fixture
    def repo(self, tmp_path: Path) -> RunHistoryRepository:
        return RunHistoryRepository(db_path=tmp_path / "fill.db")

    def _add(
        self,
        repo: RunHistoryRepository,
        *,
        article_id: str,
        days_ago: float,
        category: str = "apt",
        intent: str | None = None,
    ) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id=article_id,
                title=f"t-{article_id}",
                url=f"https://example.com/{article_id}",
                feed_title="f",
                summary="s",
                importance="medium",
                posted_channel="watch",
                status="posted",
                category=category,
                socio_political_intent=intent,
                created_at=datetime.now(UTC) - timedelta(days=days_ago),
            ),
        )

    def test_fetch_daily_rows_counts_coverage(self, repo: RunHistoryRepository) -> None:
        # Arrange
        self._add(repo, article_id="a1", days_ago=1, intent="espionage")
        self._add(repo, article_id="a2", days_ago=1, intent=None)
        self._add(repo, article_id="a3", days_ago=1, intent="unknown")

        from src.storage.db_backend import connect

        con = connect(repo.db_path)
        try:
            metric = next(m for m in METRICS if m.key == "intent")

            # Act
            rows = fetch_daily_rows(
                con, metric, (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
            )
        finally:
            con.close()

        # Assert: 3 件中 espionage の 1 件のみ filled
        assert len(rows) == 1
        assert rows[0][1] == 3
        assert rows[0][2] == 1

    def test_collect_weekly_cells_all_metrics_run(self, repo: RunHistoryRepository) -> None:
        # Arrange: entity 系 metric の EXISTS も SQL エラーにならないことを検証
        self._add(repo, article_id="a1", days_ago=1, intent="espionage")

        from src.storage.db_backend import connect

        con = connect(repo.db_path)
        try:
            # Act
            cells = collect_weekly_cells(con, now=datetime.now(UTC))
        finally:
            con.close()

        # Assert
        assert set(cells.keys()) == {m.key for m in METRICS}

    def test_heartbeat_fill_line(self, repo: RunHistoryRepository) -> None:
        # Arrange
        self._add(repo, article_id="a1", days_ago=1, intent="espionage")

        # Act
        line = build_heartbeat_fill_line(repo.db_path)

        # Assert
        assert line is not None
        assert "抽出7日" in line
        # 番兵 3 指標が並ぶ
        assert len(SENTINEL_KEYS) == 3


class TestHeartbeatIntegration:
    def test_heartbeat_text_includes_fill_and_standing_lines(self) -> None:
        from src.ui.services.source_health import build_heartbeat_text

        title, body, importance = build_heartbeat_text(
            run_counts={"succeeded": 5},
            silent=[],
            feeds_total=100,
            fill_line="抽出7日: intent 60% / 技術結線 15% / event_date(cyber) 40%",
            standing_line="standing: 4件 証拠30d 9 / 最終評価 2026-07-13",
        )

        assert "抽出7日" in body
        assert "standing: 4件" in body
        assert importance == "low"

    def test_fill_warn_escalates_importance(self) -> None:
        from src.ui.services.source_health import build_heartbeat_text

        _, _, importance = build_heartbeat_text(
            run_counts={"succeeded": 5},
            silent=[],
            feeds_total=100,
            fill_line="⚠️ 抽出7日: intent 2%⚠️ / 技術結線 15% / event_date(cyber) 40%",
        )

        assert importance == "medium"


class TestFeedBodyHealthCollapse:
    """B4: source (feed) 単位の本文取得健全性 急落検知。"""

    def test_detects_per_feed_collapse(self) -> None:
        # BleepingComputer 型: 過去 ~50% → 直近週 0% に崩壊
        cells = {
            "BleepingComputer": _cells(
                "BleepingComputer",
                [
                    ("2026-06-22", 30, 15),
                    ("2026-06-29", 30, 15),
                    ("2026-07-06", 30, 14),
                    ("2026-07-13", 30, 15),
                    ("2026-07-20", 30, 0),
                ],
            ),
            "StableFeed": _cells(
                "StableFeed",
                [("2026-07-13", 30, 27), ("2026-07-20", 30, 26)],
            ),
        }
        warns = detect_feed_body_health_collapses(cells, eval_week=date(2026, 7, 20))
        labels = {w.label for w in warns}
        assert "BleepingComputer" in labels  # 急落を検知
        assert "StableFeed" not in labels  # 安定は騒がせない

    def test_ignores_small_sample_feed(self) -> None:
        # 直近週 n < _FEED_MIN_WEEK_N(20) は母数ノイズとして判定しない
        cells = {
            "Tiny": _cells(
                "Tiny",
                [("2026-07-06", 25, 20), ("2026-07-13", 25, 20), ("2026-07-20", 10, 0)],
            )
        }
        assert detect_feed_body_health_collapses(cells, eval_week=date(2026, 7, 20)) == []

    def test_build_lines_empty_when_no_warns(self) -> None:
        assert build_feed_body_health_lines([], feeds_checked=100) == []

    def test_build_lines_has_header_and_rows(self) -> None:
        warns = detect_feed_body_health_collapses(
            {
                "Feed X": _cells(
                    "Feed X",
                    [
                        ("2026-06-29", 30, 15),
                        ("2026-07-06", 30, 15),
                        ("2026-07-13", 30, 15),
                        ("2026-07-20", 30, 1),
                    ],
                )
            },
            eval_week=date(2026, 7, 20),
        )
        lines = build_feed_body_health_lines(warns, feeds_checked=137)
        assert lines
        assert "ソースが急落" in lines[0]
        assert any("Feed X" in line for line in lines)
