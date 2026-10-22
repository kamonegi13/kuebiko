"""常設の問いの根拠の候補の範囲 (STANDING_EVIDENCE_SCOPE)。

(a) 国家の活動を問う問いは国家系アクターに結び付く記事だけを候補にする
    (辞書 nation=ru でも ransom_group 等の非国家だけが根拠の記事は外す)。
(b) 破壊・妨害の問いは、サイバーでない記事 (geopolitical / policy / other) を候補にしない。
旗 0 で旧挙動。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.assessment.situation_store import SituationStore
from src.assessment.standing import (
    ensure_standing_situations,
    harvest_standing_evidence,
)
from src.assessment.standing_scope import (
    QuestionScope,
    category_in_scope,
    scope_enabled,
    scope_for_question,
)
from src.storage.run_history import RunHistoryRepository

_NOW = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
_NOW_ISO = _NOW.isoformat()

_TREND_DISRUPTION = {"frame_id": "trend", "slots": {"threat": "disruption"}}
_TREND_ESPIONAGE = {"frame_id": "trend", "slots": {"threat": "espionage"}}
_THRESHOLD_RU = {"frame_id": "threshold", "slots": {"subject": "ru"}}


class TestScopeDerivation:
    def test_flag_default_on_and_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STANDING_EVIDENCE_SCOPE", raising=False)
        assert scope_enabled() is True
        monkeypatch.setenv("STANDING_EVIDENCE_SCOPE", "0")
        assert scope_enabled() is False

    def test_state_subject_question_is_state_only(self) -> None:
        assert scope_for_question(_THRESHOLD_RU) == QuestionScope(state_only=True, cyber_only=False)

    def test_disruption_trend_is_cyber_only(self) -> None:
        assert scope_for_question(_TREND_DISRUPTION) == QuestionScope(
            state_only=False, cyber_only=True
        )

    def test_espionage_trend_is_unscoped(self) -> None:
        assert scope_for_question(_TREND_ESPIONAGE) == QuestionScope(False, False)

    def test_flag_off_means_unscoped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STANDING_EVIDENCE_SCOPE", "0")
        assert scope_for_question(_THRESHOLD_RU) == QuestionScope(False, False)
        assert scope_for_question(_TREND_DISRUPTION) == QuestionScope(False, False)

    def test_non_cyber_categories_are_out_of_cyber_scope(self) -> None:
        cyber = QuestionScope(state_only=False, cyber_only=True)
        for cat in ("geopolitical", "policy", "other"):
            assert category_in_scope(cyber, cat) is False
        for cat in ("apt", "breach", "incident", "malware", "vulnerability", "advisory"):
            assert category_in_scope(cyber, cat) is True

    def test_unscoped_accepts_any_category(self) -> None:
        assert category_in_scope(QuestionScope(False, False), "geopolitical") is True


@pytest.fixture
def store(tmp_path: Path) -> SituationStore:
    db = tmp_path / "scope.db"
    RunHistoryRepository(db_path=db)
    return SituationStore(db_path=db)


def _article(
    store: SituationStore,
    aid: str,
    *,
    intent: str = "prepositioning",
    category: str = "apt",
    victim_country: str | None = None,
    victim_sector: str | None = None,
    entities: tuple[tuple[str, str], ...] = (),
) -> None:
    with store._repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (started_at, pipeline, dry_run, status) VALUES (?, 't', 0, 'done')",
            (_NOW_ISO,),
        )
        rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (run_id, article_id, title, url, status, created_at,"
            " socio_political_intent, victim_country_iso, victim_sector_canonical, category)"
            " VALUES (?,?,?,?, 'posted', ?, ?, ?, ?, ?)",
            (
                rid,
                aid,
                f"t-{aid}",
                f"https://x/{aid}",
                _NOW_ISO,
                intent,
                victim_country,
                victim_sector,
                category,
            ),
        )
        for etype, value in entities:
            conn.execute(
                "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
                " VALUES (?,?,?,?)",
                (aid, etype, value, _NOW_ISO),
            )


def _harvest(store: SituationStore) -> int:
    return harvest_standing_evidence(
        store=store,
        repo=store._repo,  # noqa: SLF001
        db_path=store._repo.db_path,  # noqa: SLF001
        now_iso=_NOW_ISO,
        lookback_hours=48,
    )


def _ids(store: SituationStore, sid: str) -> set[str]:
    with store._repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT article_id FROM situation_evidence WHERE situation_id=?", (sid,)
        ).fetchall()
    return {str(r[0]) for r in rows}


class TestSeedHarvest:
    """seed (prepos-*) の R1-R3。"""

    def _setup(self, store: SituationStore) -> None:
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        # 国家系 (apt28 = bear)、犯罪 (qilin = ransom_group)、アクター無し (関与国のみ)
        _article(store, "state", entities=(("actor", "apt28"),))
        _article(
            store,
            "crime",
            victim_country="JP",
            victim_sector="transportation",
            intent="espionage",
            entities=(("actor", "qilin"),),
        )
        _article(store, "involved_only", entities=(("involved_country", "RU"),))

    def test_scope_on_keeps_state_actor_only(
        self, store: SituationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STANDING_EVIDENCE_SCOPE", "1")
        self._setup(store)
        _harvest(store)
        assert _ids(store, "s-standing-prepos-ru") == {"state"}

    def test_scope_off_is_legacy(
        self, store: SituationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STANDING_EVIDENCE_SCOPE", "0")
        self._setup(store)
        _harvest(store)
        assert _ids(store, "s-standing-prepos-ru") == {"state", "crime", "involved_only"}


class TestDeclarativeHarvest:
    def _question(self, sid: str, frame: dict[str, Any], cond: dict[str, Any]) -> dict[str, Any]:
        return {"situation_id": sid, **frame, "evidence_condition": cond}

    def _open(self, store: SituationStore, sid: str) -> None:
        store.open_situation(
            situation_id=sid,
            title="q",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=_NOW_ISO,
            kind="standing",
        )

    def _run(self, store: SituationStore, q: dict[str, Any]) -> None:
        from src.assessment.standing import _harvest_declarative

        _harvest_declarative(
            store=store,
            repo=store._repo,  # noqa: SLF001
            db_path=store._repo.db_path,  # noqa: SLF001
            now_iso=_NOW_ISO,
            since_iso="2026-10-01T00:00:00+00:00",
            questions=[q],
        )

    def test_state_question_drops_crime_only_article(
        self, store: SituationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STANDING_EVIDENCE_SCOPE", "1")
        sid = "s-standing-q-ru"
        self._open(store, sid)
        _article(store, "state", intent="espionage", entities=(("actor", "apt28"),))
        _article(
            store, "crime", intent="espionage", victim_country="JP", entities=(("actor", "qilin"),)
        )
        _article(
            store,
            "both",
            intent="espionage",
            victim_country="JP",
            entities=(("actor", "qilin"), ("actor", "apt28")),
        )
        cond = {"all": [{"property": "actor_nation", "op": "in", "value": ["ru"]}]}
        self._run(store, self._question(sid, _THRESHOLD_RU, cond))
        assert _ids(store, sid) == {"state", "both"}

    def test_state_question_flag_off_keeps_crime(
        self, store: SituationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STANDING_EVIDENCE_SCOPE", "0")
        sid = "s-standing-q-ru"
        self._open(store, sid)
        _article(store, "crime", intent="espionage", entities=(("actor", "qilin"),))
        cond = {"all": [{"property": "actor_nation", "op": "in", "value": ["ru"]}]}
        self._run(store, self._question(sid, _THRESHOLD_RU, cond))
        assert _ids(store, sid) == {"crime"}

    def test_disruption_question_drops_non_cyber_category(
        self, store: SituationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STANDING_EVIDENCE_SCOPE", "1")
        sid = "s-standing-q-dis"
        self._open(store, sid)
        _article(store, "cyber", intent="disruption", category="incident", victim_country="JP")
        _article(
            store,
            "arson",
            intent="disruption",
            category="geopolitical",
            victim_country="PL",
            victim_sector="telecom",
        )
        _article(
            store,
            "global_ci",
            intent="disruption",
            category="incident",
            victim_country="PL",
            victim_sector="energy",
        )
        cond = {"all": [{"property": "intent", "op": "eq", "value": "disruption"}]}
        self._run(store, self._question(sid, _TREND_DISRUPTION, cond))
        # 世界の CI へのサイバー事案は間接の根拠として候補に残る
        assert _ids(store, sid) == {"cyber", "global_ci"}
