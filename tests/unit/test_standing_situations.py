"""常設情報要求 standing situations (段A: 器と収穫) の unit test。

設計 (docs/prepositioning_posture_ledger_design.md) の受入基準:
seed 開設が冪等 / dormant にならない / 収穫 R1-R3 が辞書ゲート帰属のみで働く /
cap 超過は detection_log に記録 / flag off で不活性。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.assessment.ledger import _sweep_lifecycle
from src.assessment.situation_store import SituationStore
from src.assessment.standing import (
    STANDING_SEEDS,
    ensure_standing_situations,
    harvest_standing_evidence,
    standing_enabled,
)
from src.storage.run_history import RunHistoryRepository

_NOW = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
_NOW_ISO = _NOW.isoformat()


@pytest.fixture
def store(tmp_path: Path) -> SituationStore:
    db = tmp_path / "standing.db"
    RunHistoryRepository(db_path=db)  # schema 初期化
    return SituationStore(db_path=db)


def _seed_article(
    store: SituationStore,
    *,
    article_id: str,
    intent: str | None,
    victim_country: str | None = None,
    victim_sector: str | None = None,
    entities: tuple[tuple[str, str], ...] = (),
    created_at: str = _NOW_ISO,
) -> None:
    """収穫の入力 (articles + article_entities) を直接 seed する (test 専用)。"""
    with store._repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (started_at, pipeline, dry_run, status) VALUES (?, 't', 0, 'done')",
            (created_at,),
        )
        rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (run_id, article_id, title, url, status, created_at,"
            " socio_political_intent, victim_country_iso, victim_sector_canonical)"
            " VALUES (?,?,?,?, 'posted', ?, ?, ?, ?)",
            (
                rid,
                article_id,
                f"title-{article_id}",
                f"https://x/{article_id}",
                created_at,
                intent,
                victim_country,
                victim_sector,
            ),
        )
        for etype, value in entities:
            conn.execute(
                "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
                " VALUES (?,?,?,?)",
                (article_id, etype, value, created_at),
            )


def _harvest(store: SituationStore, **kw: object) -> int:
    return harvest_standing_evidence(
        store=store,
        repo=store._repo,  # noqa: SLF001
        db_path=store._repo.db_path,  # noqa: SLF001
        now_iso=_NOW_ISO,
        lookback_hours=48,
        **kw,
    )


def _evidence(store: SituationStore, sid: str) -> list[str]:
    with store._repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT article_id FROM situation_evidence"
            " WHERE situation_id=? AND assigned_by='standing'",
            (sid,),
        ).fetchall()
    return [str(r[0]) for r in rows]


class TestSeeds:
    def test_ensure_is_idempotent_with_stable_ids(self, store: SituationStore) -> None:
        assert ensure_standing_situations(store=store, now_iso=_NOW_ISO) == 4
        assert ensure_standing_situations(store=store, now_iso=_NOW_ISO) == 0
        rows = [r for r in store.load_situations(("active",)) if r.kind == "standing"]
        assert len(rows) == 4
        assert {r.situation_id for r in rows} == {s.situation_id for s in STANDING_SEEDS}

    def test_standing_never_goes_dormant(self, store: SituationStore) -> None:
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        # 通常の event situation は 30 日 idle で dormant になる条件を作る
        store.open_situation(
            title="単発の事象",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=(_NOW - timedelta(days=40)).isoformat(),
        )
        dormant, closed = _sweep_lifecycle(store=store, now=_NOW + timedelta(days=40))
        assert dormant == 1  # event のみ
        standing = [r for r in store.load_situations(("active",)) if r.kind == "standing"]
        assert len(standing) == 4  # standing は 40+80 日 idle でも active のまま

    def test_flag_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STANDING_SITUATIONS", raising=False)
        assert standing_enabled() is False
        monkeypatch.setenv("STANDING_SITUATIONS", "1")
        assert standing_enabled() is True


class TestHarvest:
    def test_r1_prepositioning_by_involved_country(self, store: SituationStore) -> None:
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        _seed_article(
            store,
            article_id="a1",
            intent="prepositioning",
            entities=(("involved_country", "CN"),),
        )
        _seed_article(store, article_id="a2", intent="prepositioning")  # 国なし → 不収穫
        added = _harvest(store)
        assert added == 1
        assert _evidence(store, "s-standing-prepos-cn") == ["a1"]
        assert _evidence(store, "s-standing-prepos-ru") == []

    def test_r2_adjacent_intent_needs_dictionary_gated_actor(self, store: SituationStore) -> None:
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        # volt_typhoon (実辞書 id) は nation=cn かつ doctrine 該当 (公的勧告接地)
        _seed_article(
            store,
            article_id="b1",
            intent="espionage",
            victim_sector="energy",
            entities=(("actor", "volt_typhoon"),),
        )
        # 辞書に無いアクターは nation 解決されず不収穫 (辞書ゲートの確定原則)
        _seed_article(
            store,
            article_id="b2",
            intent="espionage",
            victim_sector="energy",
            entities=(("actor", "totally-unknown-group"),),
        )
        _harvest(store)
        cn = _evidence(store, "s-standing-prepos-cn")
        assert "b1" in cn
        assert "b2" not in cn

    def test_r3_jp_victim_requires_attribution(self, store: SituationStore) -> None:
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        _seed_article(
            store,
            article_id="c1",
            intent=None,
            victim_country="JP",
            victim_sector="energy",
            entities=(("actor", "volt_typhoon"),),
        )
        # 帰属なしの JP 事象は standing に入れない (過剰帰属の再発防止)
        _seed_article(
            store,
            article_id="c2",
            intent=None,
            victim_country="JP",
            victim_sector="energy",
        )
        _harvest(store)
        cn = _evidence(store, "s-standing-prepos-cn")
        assert "c1" in cn
        assert "c2" not in cn

    def test_harvest_is_idempotent(self, store: SituationStore) -> None:
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        _seed_article(
            store,
            article_id="d1",
            intent="prepositioning",
            entities=(("involved_country", "RU"),),
        )
        assert _harvest(store) == 1
        assert _harvest(store) == 0  # 既存ペアは冪等 skip

    def test_cap_truncation_is_logged_to_detection_log(self, store: SituationStore) -> None:
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        for i in range(35):
            _seed_article(
                store,
                article_id=f"e{i}",
                intent="prepositioning",
                entities=(("involved_country", "KP"),),
            )
        added = _harvest(store)
        assert added == 30  # cap
        with store._repo._connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT reason FROM situation_detection_log WHERE reason LIKE 'standing_cap:%'"
            ).fetchone()
        assert row is not None
        assert "s-standing-prepos-kp" in str(row[0])

    def test_no_seeds_opened_means_no_harvest(self, store: SituationStore) -> None:
        _seed_article(
            store,
            article_id="f1",
            intent="prepositioning",
            entities=(("involved_country", "CN"),),
        )
        assert _harvest(store) == 0


class TestStageB:
    """段B: 予約枠キュー選定 / staleness / JP 直接証拠 cap / POSTURE フレーム。"""

    def test_posture_hypotheses_are_known(self) -> None:
        from src.synthesis.grounded.hypotheses import (
            POSTURE_HYPOTHESES,
            hypotheses_for_domain,
            is_known_hypothesis,
        )

        ids = [h.id for h in POSTURE_HYPOTHESES]
        assert "posture_active_prepositioning_jp" in ids
        assert "reporting_artifact" in ids  # SHARED を含む
        assert all(is_known_hypothesis(i) for i in ids)
        # event の domain 選択には POSTURE を漏らさない
        assert "posture_active_prepositioning_jp" not in [
            h.id for h in hypotheses_for_domain("unknown_domain")
        ]

    def test_select_prefers_initial_then_unassessed_then_stale(self, store: SituationStore) -> None:
        from src.assessment.standing import select_standing_reassessments

        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        rows = [r for r in store.load_situations(("active",)) if r.kind == "standing"]
        # cn=初回 (rev なし) / ru=未評価 2 件 / kp=stale (8 日前判定) / ir=新鮮な判定 (対象外)
        latest_rev_at = {
            "s-standing-prepos-ru": (_NOW - timedelta(days=1)).isoformat(),
            "s-standing-prepos-kp": (_NOW - timedelta(days=8)).isoformat(),
            "s-standing-prepos-ir": (_NOW - timedelta(days=1)).isoformat(),
        }
        unassessed = {"s-standing-prepos-ru": ["a1", "a2"]}
        sel = select_standing_reassessments(
            standing_rows=rows,
            unassessed=unassessed,
            latest_rev_at=latest_rev_at,
            now_iso=_NOW_ISO,
            reserve=2,
        )
        assert sel == ["s-standing-prepos-cn", "s-standing-prepos-ru"]
        sel3 = select_standing_reassessments(
            standing_rows=rows,
            unassessed=unassessed,
            latest_rev_at=latest_rev_at,
            now_iso=_NOW_ISO,
            reserve=3,
        )
        assert sel3[2] == "s-standing-prepos-kp"  # stale は新着ゼロでも候補
        assert "s-standing-prepos-ir" not in sel3  # 新鮮 + 新着なしは対象外

    def test_standing_reserve_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.assessment.standing import standing_reserve

        monkeypatch.delenv("STANDING_RESERVE", raising=False)
        assert standing_reserve() == 2
        monkeypatch.setenv("STANDING_RESERVE", "4")
        assert standing_reserve() == 4

    def test_has_jp_direct_evidence(self, store: SituationStore) -> None:
        from src.assessment.standing import has_jp_direct_evidence

        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        db = store._repo.db_path  # noqa: SLF001
        assert has_jp_direct_evidence(db_path=db, situation_id="s-standing-prepos-cn") is False
        _seed_article(
            store,
            article_id="jp1",
            intent=None,
            victim_country="JP",
            victim_sector="energy",
            entities=(("actor", "volt_typhoon"),),
        )
        _harvest(store)  # R3 で収穫される
        assert has_jp_direct_evidence(db_path=db, situation_id="s-standing-prepos-cn") is True

    def test_evidence_article_ids_ordered_and_limited(self, store: SituationStore) -> None:
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        sid = "s-standing-prepos-cn"
        for i in range(3):
            store.record_assignment(
                situation_id=sid,
                article_id=f"z{i}",
                added_at=(_NOW + timedelta(minutes=i)).isoformat(),
                assigned_by="standing",
            )
        ids = store.evidence_article_ids(sid, limit=2)
        assert ids == ["z2", "z1"]


class TestHarvestCalibration:
    """較正 (07-13): recap 除外 / R2 は観測 NISC 分野必須。"""

    def test_recap_category_is_excluded(self, store: SituationStore) -> None:
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        with store._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO runs (started_at, pipeline, dry_run, status)"
                " VALUES (?, 't', 0, 'done')",
                (_NOW_ISO,),
            )
            rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            conn.execute(
                "INSERT INTO articles (run_id, article_id, title, url, status, created_at,"
                " category, socio_political_intent)"
                " VALUES (?, 'r1', 't', 'https://x/r1', 'posted', ?, 'recap', 'prepositioning')",
                (rid, _NOW_ISO),
            )
            conn.execute(
                "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
                " VALUES ('r1', 'involved_country', 'CN', ?)",
                (_NOW_ISO,),
            )
        assert _harvest(store) == 0  # recap は一次証拠でない

    def test_r2_requires_observed_ci_sector_not_actor_mention_alone(
        self, store: SituationStore
    ) -> None:
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        # doctrine 該当アクターの言及のみ (sector NULL) — 較正後は収穫しない
        _seed_article(
            store,
            article_id="n1",
            intent="espionage",
            entities=(("actor", "volt_typhoon"),),
        )
        _harvest(store)
        assert "n1" not in _evidence(store, "s-standing-prepos-cn")


class TestPostureCards:
    """段C: posture カード = revisions/evidence の忠実な射影 (別集計を作らない)。"""

    def test_cards_reflect_revisions_and_evidence(self, store: SituationStore) -> None:
        from src.assessment.situation_store import RevisionRow
        from src.ui.services.standing_posture import build_standing_posture

        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        store.add_revision(
            RevisionRow(
                situation_id="s-standing-prepos-cn",
                rev=0,
                claim="評価文",
                claim_type="structural",
                leading_hypothesis="posture_global_no_jp_evidence",
                confidence="moderate",
                confidence_basis="ACH=moderate / source_basis=high",
                hypotheses_json="[]",
                assumptions_json="[]",
                missing_json="[]",
                indicators_json="[]",
                implication="",
                delta_type="opened",
                delta_note="",
                created_at=_NOW_ISO,
            )
        )
        _seed_article(
            store,
            article_id="p1",
            intent=None,
            victim_country="JP",
            victim_sector="energy",
            entities=(("actor", "volt_typhoon"),),
        )
        _harvest(store)

        cards = build_standing_posture(db_path=store._repo.db_path, now=_NOW)  # noqa: SLF001
        assert [c["nation"] for c in cards] == ["cn", "kp", "ru", "ir"]  # seed 順
        cn = cards[0]
        assert cn["assessed"] is True
        assert cn["leading_label"] == "世界的に活動・日本標的の直接証拠なし"
        assert cn["confidence"] == "moderate"
        # 確度の根拠 (ACH + source_basis) が台帳から辿れる (honesty doctrine: 確度は接地を可視化)
        assert cn["confidence_basis"] == "ACH=moderate / source_basis=high"
        assert cn["evidence_direct_30d"] == 1
        assert cn["trajectory"][-1]["delta_type"] == "opened"
        kp = next(c for c in cards if c["nation"] == "kp")
        assert kp["assessed"] is False
        assert kp["evidence_related_30d"] == 0
