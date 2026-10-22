"""常設の問い「日本の重要インフラに対する金銭目的 (犯罪組織) は悪化しているか」(2026-10-22)。

- 型は趨勢 (trend) × 脅威 = financial。根拠の候補は **非国家の犯罪アクター (ransom_group /
  spider) に結び付き、サイバーの事案** のものだけ (STANDING_EVIDENCE_SCOPE の型に載せる)。
- 投入用の台本は opt-in (既定の `promote_standing_questions.py --apply` では入らない)。
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from src.assessment.question_draft import qualify, question_text
from src.assessment.question_store import QUESTIONS_KEY
from src.assessment.situation_store import SituationStore
from src.assessment.standing import _harvest_declarative
from src.assessment.standing_scope import (
    QuestionScope,
    criminal_actor_in_scope,
    scope_for_question,
)
from src.storage.run_history import RunHistoryRepository

_ROOT = Path(__file__).resolve().parents[2]
_NOW_ISO = datetime(2026, 10, 22, 6, 0, tzinfo=UTC).isoformat()
_SID = "s-standing-q-crime"
_TREND_FINANCIAL = {"frame_id": "trend", "slots": {"threat": "financial"}}


def _load_script() -> ModuleType:
    path = _ROOT / "scripts" / "promote_standing_questions.py"
    spec = importlib.util.spec_from_file_location("promote_standing_questions_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDraft:
    def test_script_exposes_opt_in_draft_not_in_default_set(self) -> None:
        mod = _load_script()
        assert "criminal-financial" in mod.OPT_IN_DRAFTS
        assert mod.OPT_IN_DRAFTS["criminal-financial"] not in mod.DRAFTS

    def test_draft_passes_qualification_gate(self) -> None:
        draft = _load_script().OPT_IN_DRAFTS["criminal-financial"]
        result = qualify(draft)
        assert result.ok, result.failures

    def test_question_text_names_japan_ci_and_financial(self) -> None:
        draft = _load_script().OPT_IN_DRAFTS["criminal-financial"]
        text = question_text(draft)
        assert "日本" in text and "重要インフラ" in text and "金銭" in text

    def test_evidence_has_direct_and_indirect_arms(self) -> None:
        cond = _load_script().OPT_IN_DRAFTS["criminal-financial"].evidence_condition
        arms = cond["any"]
        assert len(arms) == 2
        props = [{leaf["property"] for leaf in arm["all"]} for arm in arms]
        assert {"intent", "victim_country"} in props  # 直接 = 日本被害
        assert {"intent", "victim_sector"} in props  # 間接 = 世界の CI

    def test_hypotheses_come_from_trend_frame(self, tmp_path: Path, monkeypatch: Any) -> None:
        from src.assessment.question_store import hypotheses_for_standing, promote
        from src.storage import config_store
        from src.synthesis.grounded.hypotheses import TREND_HYPOTHESES

        db = tmp_path / "cfg.db"
        monkeypatch.setattr(config_store, "_DEFAULT_DB", db, raising=False)

        class _Store:
            def get_situation(self, sid: str) -> Any:
                return None

            def open_situation(self, **kw: Any) -> None:
                return None

        draft = _load_script().OPT_IN_DRAFTS["criminal-financial"]
        sid = promote(draft, store=_Store(), now_iso=_NOW_ISO, db_path=db)
        assert QUESTIONS_KEY == "standing_questions"
        assert hypotheses_for_standing(sid, db_path=db) == TREND_HYPOTHESES


class TestScope:
    def test_financial_trend_is_criminal_and_cyber_scoped(self) -> None:
        assert scope_for_question(_TREND_FINANCIAL) == QuestionScope(
            state_only=False, cyber_only=True, criminal_only=True
        )

    def test_other_trends_are_not_criminal_scoped(self) -> None:
        for threat in ("espionage", "disruption"):
            scope = scope_for_question({"frame_id": "trend", "slots": {"threat": threat}})
            assert scope.criminal_only is False

    def test_flag_off_means_unscoped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STANDING_EVIDENCE_SCOPE", "0")
        assert scope_for_question(_TREND_FINANCIAL) == QuestionScope(False, False, False)

    def test_criminal_actor_membership(self) -> None:
        scope = QuestionScope(state_only=False, cyber_only=True, criminal_only=True)
        assert criminal_actor_in_scope(scope, frozenset({"actor:qilin"})) is True
        assert criminal_actor_in_scope(scope, frozenset({"actor:scattered_spider"})) is True
        assert criminal_actor_in_scope(scope, frozenset({"actor:apt28"})) is False
        assert criminal_actor_in_scope(scope, frozenset({"actor:killnet"})) is False
        assert criminal_actor_in_scope(scope, frozenset()) is False

    def test_unscoped_does_not_require_actor(self) -> None:
        assert criminal_actor_in_scope(QuestionScope(False, False), frozenset()) is True


@pytest.fixture
def store(tmp_path: Path) -> SituationStore:
    db = tmp_path / "crime.db"
    RunHistoryRepository(db_path=db)
    return SituationStore(db_path=db)


def _article(
    store: SituationStore,
    aid: str,
    *,
    category: str = "incident",
    victim_country: str | None = "JP",
    victim_sector: str | None = None,
    actors: tuple[str, ...] = (),
    intent: str = "financial",
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
            (rid, aid, f"t-{aid}", f"https://x/{aid}", _NOW_ISO, intent, victim_country,
             victim_sector, category),
        )  # fmt: skip
        for actor in actors:
            conn.execute(
                "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
                " VALUES (?, 'actor', ?, ?)",
                (aid, actor, _NOW_ISO),
            )


def _run(store: SituationStore) -> set[str]:
    store.open_situation(
        situation_id=_SID,
        title="q",
        domain="cyber_incident",
        anchors=frozenset(),
        pir_ids=(),
        now_iso=_NOW_ISO,
        kind="standing",
    )
    cond = _load_script().OPT_IN_DRAFTS["criminal-financial"].evidence_condition
    _harvest_declarative(
        store=store,
        repo=store._repo,  # noqa: SLF001
        db_path=store._repo.db_path,  # noqa: SLF001
        now_iso=_NOW_ISO,
        since_iso="2026-10-01T00:00:00+00:00",
        questions=[{"situation_id": _SID, **_TREND_FINANCIAL, "evidence_condition": cond}],
    )
    with store._repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT article_id FROM situation_evidence WHERE situation_id=?", (_SID,)
        ).fetchall()
    return {str(r[0]) for r in rows}


class TestHarvest:
    def _seed(self, store: SituationStore) -> None:
        _article(store, "jp_ransom", actors=("qilin",))
        _article(store, "jp_spider", actors=("scattered_spider",))
        _article(
            store, "world_ci", victim_country="PL", victim_sector="energy", actors=("lockbit",)
        )
        _article(store, "state_actor", actors=("apt28",))
        _article(store, "no_actor")
        _article(store, "hacktivist", actors=("killnet",))
        _article(store, "non_cyber", category="geopolitical", actors=("qilin",))
        _article(store, "wrong_intent", intent="espionage", actors=("qilin",))
        _article(store, "world_non_ci", victim_country="PL", actors=("qilin",))

    def test_scope_on_keeps_cyber_cases_tied_to_criminal_actors(
        self, store: SituationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STANDING_EVIDENCE_SCOPE", "1")
        self._seed(store)
        assert _run(store) == {"jp_ransom", "jp_spider", "world_ci"}

    def test_scope_off_is_legacy(
        self, store: SituationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STANDING_EVIDENCE_SCOPE", "0")
        self._seed(store)
        assert _run(store) == {
            "jp_ransom",
            "jp_spider",
            "world_ci",
            "state_actor",
            "no_actor",
            "hacktivist",
            "non_cyber",
        }
