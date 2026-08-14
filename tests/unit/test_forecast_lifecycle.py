"""forecast lifecycle (open→scored) のテスト (監査 2026-07-05 P4)。

Situation の indicators は「観測されれば判定が変わる」反証可能な予測。
発火 (hit) / 期限切れ (expired) を決定論採点し、報告に射影する —
予測の説明責任 (ICD 203 較正の実測ループ) を初めて閉じる。

較正バグ修正 (2026-08-14): hit は指標を LLM に再提示してはじめて立つ。提示され
なかった指標は構造的に hit 不能なので、未発火でも外れ (expired) とは採点できない
(実測: 240 件の「外れ」のうち 36 件が一度も照会されておらず hit は 0 件)。
未照会は unevaluated として分離し、的中率の分母から除外する。

判定は「提示した事実」(presented_count) だけを見る — revision の有無からの推測は
prompt の cap で載せ切れなかった指標を照会済みと誤判定するため採らない。
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
from src.assessment.situation_store import RevisionRow, SituationStore

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


def _present(store: SituationStore, sid: str, *indicators: str) -> None:
    """指標を LLM に提示したことを記録する (= 照会の機会があった)。"""
    store.mark_forecasts_presented(sid, list(indicators))


def _seed_revision(store: SituationStore, sid: str, created_at: datetime) -> None:
    """後続 revision を 1 本足す (delta/軌跡系のテスト用)。"""
    store.add_revision(
        RevisionRow(
            situation_id=sid,
            rev=0,  # 採番は add_revision 側 (INSERT...SELECT)
            claim="c",
            claim_type="ongoing_activity",
            leading_hypothesis="h",
            confidence="moderate",
            confidence_basis="",
            hypotheses_json="[]",
            assumptions_json="[]",
            missing_json="[]",
            indicators_json="[]",
            implication="",
            delta_type="strengthened",
            delta_note="",
            created_at=created_at.isoformat(),
        )
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
        """再評価されたのに発火しなかった予測 = 真の未発現 (expired)。"""
        _seed_situation(store)
        update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": ["未発現の指標"]},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        _present(store, "s-1", "未発現の指標")
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
        assert result.unevaluated == 0
        assert store.open_forecast_rows() == []

    def test_never_presented_is_unevaluated(self, store: SituationStore) -> None:
        """一度も提示していない予測は hit 不能 → 外れではなく未照会 (unevaluated)。"""
        _seed_situation(store)
        update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": ["一度も照会されない指標"]},
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
        assert result.unevaluated == 1
        assert result.expired == 0
        scored = store.forecasts_scored_between(
            _NOW.isoformat(), (later + timedelta(days=1)).isoformat()
        )
        assert scored[0]["status"] == "unevaluated"

    def test_revision_alone_does_not_count_as_presented(self, store: SituationStore) -> None:
        """revision があっても、その指標を提示していなければ照会済みにはしない。

        prompt の cap で載せ切れなかった指標を「情勢が再評価されたから照会済み」と
        みなすと、見せていない予測を外れと採点してしまう。
        """
        _seed_situation(store)
        _seed_revision(store, "s-1", _NOW + timedelta(days=1))
        update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": ["指標"]},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        result = update_forecasts(
            store=store,
            now=_NOW + timedelta(days=FORECAST_HORIZON_DAYS + 1),
            latest_indicators_by_sid={"s-1": []},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        assert result.unevaluated == 1
        assert result.expired == 0

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
        _present(store, "s-1", "指標A", "指標B", "指標C")
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
        _present(store, "s-1", "指標X")
        result = update_forecasts(
            store=store,
            now=_NOW + timedelta(days=1),
            latest_indicators_by_sid={},
            fired_by_sid={},
            active_sids=set(),
            closed_sids={"s-1"},
        )
        assert result.expired == 1

    def test_close_without_followup_is_unevaluated(self, store: SituationStore) -> None:
        """クローズは「追跡をやめた」であって「予測が外れた」ではない。"""
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
        assert result.unevaluated == 1
        assert result.expired == 0


class TestCarryForward:
    """根本治療: open な指標を horizon の間ずっと再提示する (2026-08-14)。

    旧挙動は直前 revision の 3 件だけを提示していたため、3 件上限に押し出された
    指標が照会されないまま 30 日後に「外れ」と採点されていた。
    """

    def test_open_indicators_returned_newest_first(self, store: SituationStore) -> None:
        _seed_situation(store)
        for i, day in enumerate([0, 1, 2]):
            update_forecasts(
                store=store,
                now=_NOW + timedelta(days=day),
                latest_indicators_by_sid={"s-1": [f"指標{i}"]},
                fired_by_sid={},
                active_sids={"s-1"},
                closed_sids=set(),
            )
        assert store.open_indicators_for("s-1", limit=10) == ("指標2", "指標1", "指標0")

    def test_open_indicators_respects_limit(self, store: SituationStore) -> None:
        _seed_situation(store)
        update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": [f"指標{i}" for i in range(5)]},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        assert len(store.open_indicators_for("s-1", limit=2)) == 2

    def test_scored_indicators_are_not_carried(self, store: SituationStore) -> None:
        """発火済み/終了済みは再提示しない (open のみが持ち越し対象)。"""
        _seed_situation(store)
        update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": ["発火する指標", "残る指標"]},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        update_forecasts(
            store=store,
            now=_NOW + timedelta(days=1),
            latest_indicators_by_sid={"s-1": []},
            fired_by_sid={"s-1": ["発火する指標"]},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        assert store.open_indicators_for("s-1", limit=10) == ("残る指標",)

    def test_mark_presented_scopes_to_situation_and_open(self, store: SituationStore) -> None:
        """他情勢や採点済み行に印を付けない (誤って外れ扱いにしないための境界)。"""
        _seed_situation(store, "s-1")
        _seed_situation(store, "s-2")
        for sid in ("s-1", "s-2"):
            update_forecasts(
                store=store,
                now=_NOW,
                latest_indicators_by_sid={sid: ["共通指標"]},
                fired_by_sid={},
                active_sids={sid},
                closed_sids=set(),
            )
        assert store.mark_forecasts_presented("s-1", ["共通指標"]) == 1
        counts = {
            str(r["situation_id"]): int(r["presented_count"]) for r in store.open_forecast_rows()
        }
        assert counts == {"s-1": 1, "s-2": 0}

    def test_prior_view_merges_latest_then_carried(self) -> None:
        """直前 revision の指標を先頭に、持ち越しを重複排除して連結し cap で切る。"""
        from src.assessment.stateful import _prior_view
        from src.synthesis.grounded.incremental import CARRIED_INDICATORS_MAX

        rev = RevisionRow(
            situation_id="s-1",
            rev=1,
            claim="c",
            claim_type="ongoing_activity",
            leading_hypothesis="h",
            confidence="moderate",
            confidence_basis="",
            hypotheses_json="[]",
            assumptions_json="[]",
            missing_json="[]",
            indicators_json='["最新A", "最新B"]',
            implication="",
            delta_type="strengthened",
            delta_note="",
            created_at=_NOW.isoformat(),
        )
        view = _prior_view(rev, [], carried=("最新B", "古い指標"))
        assert view.indicators == ("最新A", "最新B", "古い指標")  # 重複 "最新B" は 1 回

        overflow = _prior_view(rev, [], carried=tuple(f"i{n}" for n in range(50)))
        assert len(overflow.indicators) == CARRIED_INDICATORS_MAX

    def test_presented_survives_until_horizon(self, store: SituationStore) -> None:
        """1 回でも提示されていれば、その後 revision が無くても真の未発現と採点する。"""
        _seed_situation(store)
        update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": ["指標"]},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        _present(store, "s-1", "指標")
        result = update_forecasts(
            store=store,
            now=_NOW + timedelta(days=FORECAST_HORIZON_DAYS + 1),
            latest_indicators_by_sid={"s-1": []},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        assert (result.expired, result.unevaluated) == (1, 0)


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

    def test_unevaluated_projected_as_own_verdict(self, store: SituationStore) -> None:
        """未照会は realized/missed のどちらでもない第 3 の verdict として射影する。

        missed に混ぜると的中率が過小評価され、realized に混ぜると過確信になる。
        """
        _seed_situation(store)
        update_forecasts(
            store=store,
            now=_NOW,
            latest_indicators_by_sid={"s-1": ["照会されない指標"]},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        later = _NOW + timedelta(days=FORECAST_HORIZON_DAYS + 1)
        update_forecasts(
            store=store,
            now=later,
            latest_indicators_by_sid={"s-1": []},
            fired_by_sid={},
            active_sids={"s-1"},
            closed_sids=set(),
        )
        ctx = build_forecast_context(
            store=store,
            period_start=_NOW,
            period_end=later + timedelta(days=1),
            now=later,
        )
        assert [s["verdict"] for s in ctx["forecast_scorecard"]] == ["unevaluated"]
        assert "未照会" in ctx["forecast_alignment"]
