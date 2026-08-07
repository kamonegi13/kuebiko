"""forecast lifecycle (open→scored) のテスト (監査 2026-07-05 P4)。

Situation の indicators は「観測されれば判定が変わる」反証可能な予測。
発火 (hit) / 期限切れ (expired) を決定論採点し、報告に射影する —
予測の説明責任 (ICD 203 較正の実測ループ) を初めて閉じる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.assessment.forecast import (
    FORECAST_HORIZON_DAYS,
    build_forecast_context,
    update_forecasts,
)
from src.assessment.situation_store import SituationStore

_NOW = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> SituationStore:
    return SituationStore(db_path=tmp_path / "fc.db")


def _seed_situation(store: SituationStore, sid: str = "s-1", status: str = "active") -> None:
    with store._repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO situations "
            "(situation_id, title, domain, status, anchors, pir_ids, opened_at, last_evidence_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sid, "t", "cyber_incident", status, "[]", "[]", _NOW.isoformat(), _NOW.isoformat()),
        )


class TestUpdateForecasts:
    def test_opens_forecast_for_new_indicator(self, store: SituationStore) -> None:
        _seed_situation(store)
        result = update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": ["同種事案の多発"]},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        assert result.opened == 1
        rows = store.open_forecast_rows()
        assert rows[0]["indicator"] == "同種事案の多発"

    def test_idempotent_open(self, store: SituationStore) -> None:
        _seed_situation(store)
        for _ in range(2):
            update_forecasts(
                store=store,
                now=_NOW,
                latest_indicators_by_sid={"s-1": ["同種事案の多発"]},
                fired_by_sid={},
                active_sids={"s-1"},
                closed_sids=set(),
            )
        assert len(store.open_forecast_rows()) == 1

    def test_fired_indicator_scored_as_hit(self, store: SituationStore) -> None:
        _seed_situation(store)
        update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": ["新たな被害公表"]},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        result = update_forecasts(
            store=store,
            now=_NOW + timedelta(days=2),
            latest_indicators_by_sid={"s-1": []},
            fired_by_sid={"s-1": ["新たな被害公表"]},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        assert result.hit == 1
        assert store.open_forecast_rows() == []
        scored = store.forecasts_scored_between(
            _NOW.isoformat(), (_NOW + timedelta(days=3)).isoformat()
        )
        assert scored[0]["status"] == "hit"

    def test_horizon_expiry_scored_as_expired(self, store: SituationStore) -> None:
        _seed_situation(store)
        update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": ["未発現の指標"]},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        later = _NOW + timedelta(days=FORECAST_HORIZON_DAYS + 1)
        result = update_forecasts(
            store=store,
            now=later,
            latest_indicators_by_sid={"s-1": []},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        assert result.expired == 1
        assert store.open_forecast_rows() == []

    def test_horizon_expiry_capped_per_run(self, store: SituationStore) -> None:
        """期限切れ採点は 1 run あたり cap で平準化する (2026-08-04 バースト対策)。

        7 月に open した ~900 件が 08-04 から一斉に期限到来すると、daily 窓の
        スコアカードが「外れ一色」に振れる。cap を超えた分は open のまま残り、
        次 run 以降で順次採点される。
        """
        _seed_situation(store)
        update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": ["指標A", "指標B", "指標C"]},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        later = _NOW + timedelta(days=FORECAST_HORIZON_DAYS + 1)
        first = update_forecasts(
            store=store,
            now=later,
            latest_indicators_by_sid={"s-1": []},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
            max_horizon_expiry=2,
        )
        assert first.expired == 2
        assert len(store.open_forecast_rows()) == 1  # 残余は open のまま次 run へ
        second = update_forecasts(
            store=store,
            now=later,
            latest_indicators_by_sid={"s-1": []},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
            max_horizon_expiry=2,
        )
        assert second.expired == 1
        assert store.open_forecast_rows() == []

    def test_closed_situation_expires_open_forecasts(self, store: SituationStore) -> None:
        _seed_situation(store)
        update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": ["指標X"]},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        result = update_forecasts(
            store=store,
            now=_NOW + timedelta(days=1),
            latest_indicators_by_sid={},
            fired_by_sid={},
            active_sids=set(),
            closed_sids={"s-1"},
        )
        assert result.expired == 1


class TestBuildForecastContext:
    def test_shapes_match_ui_contract(self, store: SituationStore) -> None:
        # UI/KPI 契約: forecasts=[{claim,horizon,confidence}] /
        # forecast_scorecard=[{claim,verdict,reason}] (verdict は realized/missed)
        _seed_situation(store)
        update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": ["新たな被害公表", "パッチ公開"]},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        update_forecasts(
            store=store,
            now=_NOW + timedelta(days=1),
            latest_indicators_by_sid={"s-1": []},
            fired_by_sid={"s-1": ["新たな被害公表"]},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        ctx = build_forecast_context(
            store=store,
            period_start=_NOW,
            period_end=_NOW + timedelta(days=2),
            now=_NOW + timedelta(days=2),
        )
        assert ctx["forecasts"][0]["horizon"] == "next_month"
        assert ctx["forecasts"][0]["confidence"] in ("high", "medium", "low")
        assert ctx["forecast_scorecard"][0]["verdict"] == "realized"
        assert "発火" in ctx["forecast_alignment"]
        assert isinstance(ctx["freshness_note"], str) and ctx["freshness_note"]

    def test_quiet_period_honest_alignment(self, store: SituationStore) -> None:
        ctx = build_forecast_context(
            store=store,
            period_start=_NOW,
            period_end=_NOW + timedelta(days=1),
            now=_NOW + timedelta(days=1),
        )
        assert ctx["forecasts"] == []
        assert ctx["forecast_scorecard"] == []
        assert "採点対象" in ctx["forecast_alignment"]
