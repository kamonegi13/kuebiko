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


class TestQuestionIsImmutable:
    """常設情報要求の**問い**は不変。動くのは答えだけ (2026-09-15)。

    増分 ACH の同一性追従 (`update_title`) は event situation では正しい — 続報で事象の
    輪郭が定まるため。しかし常設情報要求では LLM の claim は**答え**であり、それを**問い**に
    上書きすると問い自体が失われる。実測 (2026-09-15): seed 4 問のうち 3 問が別の問いへ
    変質していた (北朝鮮の問いは「日本の重要インフラ」を落として韓国金融の話になっていた)。
    """

    def test_standing_title_is_not_overwritten_by_the_answer(self, store: SituationStore) -> None:
        from src.assessment.stateful import title_follows_claim

        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        seed = STANDING_SEEDS[0]
        row = store.get_situation(seed.situation_id)
        assert row is not None

        assert title_follows_claim(row) is False

    def test_event_title_still_follows_the_claim(self, store: SituationStore) -> None:
        """event は従来どおり (続報で「〜が実施された」のまま固まらないための機構)。"""
        from src.assessment.stateful import title_follows_claim

        row = store.open_situation(
            title="ある組織への不正アクセス",
            domain="cyber_incident",
            anchors=frozenset({"victim_org:acme"}),
            pir_ids=(),
            now_iso=_NOW_ISO,
        )

        assert title_follows_claim(row) is True


class TestQuestionBriefPayload:
    """段A (PIR ブリーフの面): 問いを主語にして読むのに要る欄が揃っているか。

    設計 docs/pir_brief_design.md §3。材料はすべて台帳に既存なので、**別集計を作らず**
    既存の posture builder を拡張して両方の面が同じ射影を読む (本ファイルの既存不変条件)。
    """

    def _assessed_cn(self, store: SituationStore) -> None:
        from src.assessment.situation_store import RevisionRow

        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        store.add_revision(
            RevisionRow(
                situation_id="s-standing-prepos-cn",
                rev=0,
                claim="日本標的の直接証拠は無い",
                claim_type="structural",
                leading_hypothesis="posture_global_no_jp_evidence",
                confidence="low",
                confidence_basis="ACH=low / source_basis=medium",
                hypotheses_json="[]",
                assumptions_json="[]",
                missing_json='["JP CI での帰属済み観測"]',
                indicators_json="[]",
                implication="",
                delta_type="weakened",
                delta_note="反証が 1 件成立した",
                created_at=_NOW_ISO,
            )
        )

    def test_answer_change_and_freshness_are_exposed(self, store: SituationStore) -> None:
        from src.ui.services.standing_posture import build_standing_posture

        self._assessed_cn(store)

        cn = build_standing_posture(db_path=store._repo.db_path, now=_NOW)[0]  # noqa: SLF001
        # 問いと答えが別の欄で読める (問いは tooltip ではない)
        assert cn["question"] == STANDING_SEEDS[0].title
        assert cn["claim"] == "日本標的の直接証拠は無い"
        # なぜ動いたか (前回からの変化の理由)
        assert cn["delta_type"] == "weakened"
        assert cn["delta_note"] == "反証が 1 件成立した"
        # 何が分かっていないか
        assert cn["missing_evidence"] == ["JP CI での帰属済み観測"]
        # 鮮度 (答えがいつの証拠に基づくか)
        assert cn["last_evidence_at"]

    def test_open_indicators_are_exposed(self, store: SituationStore) -> None:
        """「何が見えれば答えが変わるか」= 指標。発火済みと未発火を区別して出す。"""
        from src.ui.services.standing_posture import build_standing_posture

        self._assessed_cn(store)
        with store._repo._connect() as conn:  # noqa: SLF001
            for ind, status in (("未発火の指標", "open"), ("発火した指標", "hit")):
                conn.execute(
                    "INSERT INTO situation_forecasts"
                    " (situation_id, indicator, opened_at, horizon_days, status)"
                    " VALUES (?, ?, ?, ?, ?)",
                    ("s-standing-prepos-cn", ind, _NOW_ISO, 30, status),
                )
            conn.commit()

        cn = build_standing_posture(db_path=store._repo.db_path, now=_NOW)[0]  # noqa: SLF001
        by_status = {i["indicator"]: i["status"] for i in cn["indicators"]}
        assert by_status == {"未発火の指標": "open", "発火した指標": "hit"}


class TestAnswerIsNotAnEcho:
    """常設情報要求の claim は**問いへの答え**であって問いの再掲ではない (2026-09-15)。

    実測: 事象用の claim 指示 (「変えないなら前回のまま返す」) が常設にも当たり、前回 claim が
    問い文そのものだったため答えがオウム返しで固定されていた (cn は 75 revision 全部が問い文)。
    散文の追記は読まれない (2026-08-27) ので、**指示そのものを差し替える**構造で直す。
    """

    def test_prompt_frames_the_question_and_asks_for_an_answer(self) -> None:
        from src.synthesis.grounded.passes import _render

        prompt = _render(
            "synthesis/ground_incremental.j2",
            situation_title="問いのタイトル",
            prior=_prior_view_stub(),
            sources=[],
            attribution_options="",
            hypotheses=(),
            question="中国は日本の重要インフラに事前配置を進めているか",
        )

        assert "継続して追う問い" in prompt
        assert "中国は日本の重要インフラに事前配置を進めているか" in prompt
        assert "問いへの答え" in prompt
        assert "問いの文をそのまま返さない" in prompt

    def test_event_prompt_is_unchanged(self) -> None:
        """事象は従来どおり (claim は実態が分かったら改訂・変えないなら前回のまま)。"""
        from src.synthesis.grounded.passes import _render

        prompt = _render(
            "synthesis/ground_incremental.j2",
            situation_title="ある事象",
            prior=_prior_view_stub(),
            sources=[],
            attribution_options="",
            hypotheses=(),
            question="",
        )

        assert "継続して追う問い" not in prompt
        assert "変えないなら前回のまま返す" in prompt

    def test_standing_question_is_resolved_from_the_seed(self) -> None:
        from src.assessment.stateful import _standing_question

        assert _standing_question(STANDING_SEEDS[0].situation_id) == STANDING_SEEDS[0].title
        assert _standing_question("s-some-event") == ""


def _prior_view_stub() -> object:
    from src.synthesis.grounded.incremental import PriorJudgmentView

    return PriorJudgmentView(
        claim="前回の答え",
        claim_type="structural",
        leading_hypothesis="posture_global_no_jp_evidence",
        confidence="low",
        hypotheses=(),
        indicators=(),
        key_excerpts=(),
    )


class TestChangeReasonAndObservation:
    """「なぜ答えが動いたか」と「何が観測されたか」を混ぜない (2026-09-15)。

    書き込み側は delta_note に発火指標を固定接頭辞つきで併記する。表示側はそれを分離して
    別の欄に置く — 混ざったままだと「答えは動いていない (no_change) のに変化理由の欄に
    『指標発火: …』が出る」誤読になる。
    """

    def test_split_separates_reason_from_fired_indicators(self) -> None:
        from src.assessment.stateful import FIRED_INDICATOR_MARKER
        from src.ui.services.standing_posture import _split_delta_note

        note = f"反証が 1 件成立した / {FIRED_INDICATOR_MARKER}指標A; 指標B"

        reason, fired = _split_delta_note(note)

        assert reason == "反証が 1 件成立した"
        assert fired == ["指標A", "指標B"]

    def test_fired_only_note_leaves_the_reason_empty(self) -> None:
        """動いていないのに観測だけあった場合、変化理由は空 (捏造しない)。"""
        from src.assessment.stateful import FIRED_INDICATOR_MARKER
        from src.ui.services.standing_posture import _split_delta_note

        reason, fired = _split_delta_note(f"{FIRED_INDICATOR_MARKER}指標A")

        assert reason == ""
        assert fired == ["指標A"]

    def test_plain_note_is_untouched(self) -> None:
        from src.ui.services.standing_posture import _split_delta_note

        assert _split_delta_note("被害・標的の拡大を観測") == ("被害・標的の拡大を観測", [])


class TestFirstAnswerIsNotTheQuestion:
    """常設の初回評価で「答え = 問い」を焼き付けない (2026-09-15)。

    ``ground_and_score`` は渡された claim をそのまま判定文にするため、問いを渡すと 1 版目が
    オウム返しになり、増分 ACH の「変えないなら前回のまま」がそれを永続化する
    (実測: cn は 75 版すべてが問い文だった)。初回はリード仮説のラベルを答えに置く。
    """

    def test_standing_first_answer_is_the_hypothesis_label(self) -> None:
        from src.assessment.stateful import first_answer

        answer = first_answer(
            title=STANDING_SEEDS[0].title,
            leading="posture_global_no_jp_evidence",
            is_standing=True,
        )

        assert answer == "世界的に活動・日本標的の直接証拠なし"
        assert answer != STANDING_SEEDS[0].title

    def test_unknown_hypothesis_falls_back_to_the_id(self) -> None:
        from src.assessment.stateful import first_answer

        assert first_answer(title="問い", leading="未知の仮説", is_standing=True) == "未知の仮説"

    def test_event_first_answer_is_the_title(self) -> None:
        """事象は従来どおり title が判定文 (事象名 = 判定の主語)。"""
        from src.assessment.stateful import first_answer

        assert first_answer(title="ある事象", leading="criminal_financial", is_standing=False) == (
            "ある事象"
        )


class TestPostureCoversPromotedQuestions:
    """段B-3e: 昇格した問いも問いの面に出ること (読み取り側が seed 駆動だった)。

    ⚠ 実際に起きた: 問い 5 件を昇格させて台帳には入ったのに、
    `build_standing_posture` が `STANDING_SEEDS` (code 所有 4 件) を列挙していたため
    UI に 1 件も出なかった。§6e「問いはデータ」に読み取り側が追従していなかった。
    """

    def test_all_standing_situations_are_returned_by_default(
        self, store: SituationStore
    ) -> None:
        from src.ui.services.standing_posture import build_standing_posture

        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        store.open_situation(
            situation_id="s-standing-q-test",
            title="日本の重要インフラに対する破壊・妨害は悪化しているか",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=_NOW_ISO,
            kind="standing",
        )

        ids = {
            c["situation_id"]
            for c in build_standing_posture(db_path=store._repo.db_path, now=_NOW)  # noqa: SLF001
        }

        assert "s-standing-q-test" in ids
        assert "s-standing-prepos-cn" in ids

    def test_board_asks_for_seeds_only(self, store: SituationStore) -> None:
        """重要インフラ board は**事前配置 posture の面**なので 4 国だけを見る。

        全件返すと、趨勢・閾値の問いが国別 board に混ざって面の意味が壊れる。
        """
        from src.ui.services.standing_posture import build_standing_posture

        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        store.open_situation(
            situation_id="s-standing-q-test2",
            title="ロシアによる日本の重要インフラへの活動は平時の水準を越えたか",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=_NOW_ISO,
            kind="standing",
        )

        ids = {
            c["situation_id"]
            for c in build_standing_posture(
                db_path=store._repo.db_path,  # noqa: SLF001
                now=_NOW,
                seed_only=True,
            )
        }

        assert "s-standing-q-test2" not in ids
        assert len(ids) == 4

    def test_event_situations_are_never_included(self, store: SituationStore) -> None:
        """kind='event' を混ぜない (問いの面は問いだけ)。"""
        from src.ui.services.standing_posture import build_standing_posture

        store.open_situation(
            title="ある事象",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=_NOW_ISO,
        )

        cards = build_standing_posture(db_path=store._repo.db_path, now=_NOW)  # noqa: SLF001

        assert all(c["situation_id"].startswith("s-standing-") for c in cards)
