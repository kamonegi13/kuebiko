"""段B-3: 起草された問いの保存と昇格 — 単発 (RFI) → 常設情報要求。

**置き場は config_store** — 本プロジェクトの運用 config の既定パターン
(DB が正・版履歴 + revert つき、docs: operational_config_db)。問いは運用者が書く
運用設定であり、§6e「問いはデータ」に一致する。新テーブルを足さずに版履歴を得る。

⚠ `situations.domain` に型 (frame_id) を載せない — domain は既に**話題の領域**
(cyber_incident / geopolitical …) を表しており、型を重ねると語の意味が二重になる。
本プロジェクトは「standing」の多重定義で既に痛んでいる。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.assessment.question_draft import QuestionDraft
from src.assessment.question_frame import SCOPE_ALL_CI
from src.assessment.question_store import (
    QUESTIONS_KEY,
    frame_id_for,
    list_questions,
    promote,
)

_COND: dict[str, Any] = {"all": [{"property": "intent", "op": "eq", "value": "prepositioning"}]}


def _draft(frame_id: str = "trend", **over: Any) -> QuestionDraft:
    slots = (
        {"target_country": "JP", "target_scope": SCOPE_ALL_CI, "threat": "espionage"}
        if frame_id == "trend"
        else {"subject": "cn", "target_country": "JP", "target_scope": SCOPE_ALL_CI}
    )
    base: dict[str, Any] = {
        "frame_id": frame_id,
        "slots": slots,
        "decision": "防御の優先順位を変えるか",
        "evidence_condition": _COND,
        "answer_can_move_ack": True,
    }
    base.update(over)
    return QuestionDraft(**base)


class _FakeStore:
    """SituationStore の最小代役 (開設の呼ばれ方だけを見る)。"""

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []

    def get_situation(self, situation_id: str) -> None:
        return None

    def open_situation(self, **kwargs: Any) -> None:
        self.opened.append(kwargs)


@pytest.fixture
def clean_config(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """config_store を一時 DB に向ける (本番 config を汚さない)。"""
    from src.storage import config_store

    db = tmp_path / "t.db"
    monkeypatch.setattr(config_store, "_DEFAULT_DB", db, raising=False)
    return db


class TestPromotionRequiresQualification:
    def test_unqualified_draft_is_rejected(self, clean_config: Any) -> None:
        """関門を通らない問いを台帳に入れない (資格判定が昇格の前提)。"""
        store = _FakeStore()

        with pytest.raises(ValueError) as e:
            promote(
                _draft(decision=""),
                store=store,
                db_path=clean_config,
                now_iso="2026-09-16T00:00:00+00:00",
            )

        assert "決心" in str(e.value)
        assert store.opened == []

    def test_qualified_draft_opens_a_standing_situation(self, clean_config: Any) -> None:
        store = _FakeStore()

        sid = promote(
            _draft(), store=store, db_path=clean_config, now_iso="2026-09-16T00:00:00+00:00"
        )

        assert len(store.opened) == 1
        assert store.opened[0]["kind"] == "standing"
        assert store.opened[0]["situation_id"] == sid

    def test_question_text_becomes_the_title(self, clean_config: Any) -> None:
        store = _FakeStore()

        promote(_draft(), store=store, db_path=clean_config, now_iso="2026-09-16T00:00:00+00:00")

        assert (
            store.opened[0]["title"] == "日本の重要インフラに対する諜報・情報窃取は悪化しているか"
        )


class TestPersistence:
    def test_promoted_question_is_stored_as_data(self, clean_config: Any) -> None:
        """§6e — コードに焼かずデータとして持つ。"""
        store = _FakeStore()

        sid = promote(
            _draft(), store=store, db_path=clean_config, now_iso="2026-09-16T00:00:00+00:00"
        )
        rows = list_questions(db_path=clean_config)

        assert [r["situation_id"] for r in rows] == [sid]
        assert rows[0]["frame_id"] == "trend"
        assert rows[0]["decision"] == "防御の優先順位を変えるか"

    def test_frame_is_recoverable_from_situation_id(self, clean_config: Any) -> None:
        """評価側が「この問いはどの型か」を引けること。

        引けないと、趨勢の問いに事前配置の仮説で答えることになる。
        """
        store = _FakeStore()

        sid = promote(
            _draft(), store=store, db_path=clean_config, now_iso="2026-09-16T00:00:00+00:00"
        )

        assert frame_id_for(sid, db_path=clean_config) == "trend"

    def test_unknown_situation_has_no_frame(self, clean_config: Any) -> None:
        """現行 4 seed は config_store に無い — None を返して呼出側が既定へ倒せること。"""
        assert frame_id_for("s-standing-prepos-cn", db_path=clean_config) is None

    def test_promotion_is_idempotent_on_the_same_question(self, clean_config: Any) -> None:
        """同じ問いを二度昇格させても 2 件にしない (id は内容から決まる)。"""
        store = _FakeStore()
        args = {"store": store, "db_path": clean_config, "now_iso": "2026-09-16T00:00:00+00:00"}

        first = promote(_draft(), **args)
        second = promote(_draft(), **args)

        assert first == second
        assert len(list_questions(db_path=clean_config)) == 1

    def test_different_questions_get_different_ids(self, clean_config: Any) -> None:
        store = _FakeStore()
        args = {"store": store, "db_path": clean_config, "now_iso": "2026-09-16T00:00:00+00:00"}

        a = promote(_draft("trend"), **args)
        b = promote(_draft("threshold"), **args)

        assert a != b


class TestVersionHistory:
    def test_key_is_registered_for_history_and_revert(self) -> None:
        """運用 config と同じく版履歴・revert の対象にする。"""
        from src.ui.api.config_history import _KNOWN_KEYS

        assert QUESTIONS_KEY in _KNOWN_KEYS


class TestHypothesesFollowTheFrame:
    """昇格した問いは**その型の**仮説で評価されること。

    引けないと「趨勢は悪化しているか」に事前配置の仮説 (日本CIへの事前配置が進行中 /
    世界的に活動・日本標的の直接証拠なし / 別動機) で答えることになる。
    """

    def test_promoted_trend_question_uses_trend_hypotheses(self, clean_config: Any) -> None:
        from src.assessment.question_store import hypotheses_for_standing
        from src.synthesis.grounded.hypotheses import TREND_HYPOTHESES

        store = _FakeStore()
        sid = promote(
            _draft("trend"), store=store, db_path=clean_config, now_iso="2026-09-16T00:00:00+00:00"
        )

        assert hypotheses_for_standing(sid, db_path=clean_config) == TREND_HYPOTHESES

    def test_promoted_threshold_question_uses_threshold_hypotheses(self, clean_config: Any) -> None:
        from src.assessment.question_store import hypotheses_for_standing
        from src.synthesis.grounded.hypotheses import THRESHOLD_HYPOTHESES

        store = _FakeStore()
        sid = promote(
            _draft("threshold"),
            store=store,
            db_path=clean_config,
            now_iso="2026-09-16T00:00:00+00:00",
        )

        assert hypotheses_for_standing(sid, db_path=clean_config) == THRESHOLD_HYPOTHESES

    def test_legacy_seeds_keep_posture_hypotheses(self, clean_config: Any) -> None:
        """現行 4 seed は config_store に無い → 既定へ倒れる (挙動不変)。"""
        from src.assessment.question_store import hypotheses_for_standing
        from src.synthesis.grounded.hypotheses import POSTURE_HYPOTHESES

        assert (
            hypotheses_for_standing("s-standing-prepos-cn", db_path=clean_config)
            == POSTURE_HYPOTHESES
        )

    def test_store_failure_falls_back_to_posture_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """config が読めなくても評価を止めない (fail-open で現行挙動)。"""
        from src.assessment import question_store
        from src.synthesis.grounded.hypotheses import POSTURE_HYPOTHESES

        def _boom(**_: Any) -> list[dict[str, Any]]:
            raise RuntimeError("db down")

        monkeypatch.setattr(question_store, "list_questions", _boom)

        assert question_store.hypotheses_for_standing("s-x") == POSTURE_HYPOTHESES


class TestAggregateSignalSelection:
    """型によって集約シグナルの要否が決まる (A は不要、E/H は必須)。"""

    def test_trend_and_threshold_need_aggregate_signal(self, clean_config: Any) -> None:
        from src.assessment.question_store import needs_aggregate_signal

        store = _FakeStore()
        args = {"store": store, "db_path": clean_config, "now_iso": "2026-09-16T00:00:00+00:00"}
        trend = promote(_draft("trend"), **args)
        threshold = promote(_draft("threshold"), **args)

        assert needs_aggregate_signal(trend, db_path=clean_config) is True
        assert needs_aggregate_signal(threshold, db_path=clean_config) is True

    def test_legacy_presence_seeds_do_not(self, clean_config: Any) -> None:
        """A 型は 2 本腕の証拠規則で蓄積を扱っている — 集約は要らない (挙動不変)。"""
        from src.assessment.question_store import needs_aggregate_signal

        assert needs_aggregate_signal("s-standing-prepos-cn", db_path=clean_config) is False

    def test_evidence_condition_is_recoverable_for_the_signal(self, clean_config: Any) -> None:
        """集約は問いの証拠条件と同じ母集団で測る (別集計を作らない)。"""
        from src.assessment.question_store import evidence_condition_for

        store = _FakeStore()
        sid = promote(
            _draft("trend"), store=store, db_path=clean_config, now_iso="2026-09-16T00:00:00+00:00"
        )

        assert evidence_condition_for(sid, db_path=clean_config) == _COND
