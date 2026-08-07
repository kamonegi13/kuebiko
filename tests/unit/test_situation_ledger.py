"""Situation Ledger (状況台帳、段A) のテスト。

gazetteer 導出 / 決定論割当 / delta 分類 / store CRUD / update_ledger end-to-end。
設計: docs/synthesis_situation_ledger_design.md。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.assessment.assignment import (
    build_article_keys,
    build_situation_keys,
    match_situation,
)
from src.assessment.ledger import (
    _reactivate_if_dormant,
    _sweep_lifecycle,
    compute_delta_type,
    ledger_mode,
    update_ledger,
)
from src.assessment.situation_store import (
    RevisionRow,
    SituationRow,
    SituationStore,
    situation_id_for,
)
from src.cti.nation_gazetteer import nations_in_text
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.synthesis.grounded.estimate import Estimate, EvidenceItem, KeyJudgment


def _now() -> datetime:
    return datetime.now(UTC)


# ---------- nation gazetteer ----------


class TestNationGazetteer:
    def test_extracts_japanese_country_names_from_title(self) -> None:
        got = nations_in_text("ウクライナ、最新鋭戦闘機 Gripen E の導入を正式に決定")
        assert "UA" in got

    def test_extracts_us_military_compound(self) -> None:
        # involved_country 未抽出の典型例 (実測 no-anchor サンプル) を回収する
        got = nations_in_text("米空軍、B-2爆撃機による対艦ミサイル LRASM の運用能力を初公開")
        assert "US" in got

    def test_extracts_country_pair_shorthand(self) -> None:
        got = nations_in_text("米中対立の新局面")
        assert {"US", "CN"} <= got

    def test_nihongo_is_not_japan(self) -> None:
        # 「日本語」は日本への言及でない (偽陽性ガード)
        got = nations_in_text("日本語のフィッシングメールが増加")
        assert "JP" not in got

    def test_katakana_boundary_rejects_partial_match(self) -> None:
        # インドネシア != インド (カタカナ run 内部一致を弾く)
        got = nations_in_text("インドネシアの通信事業者を標的とした攻撃")
        assert "ID" in got
        assert "IN" not in got

    def test_indo_pacific_is_not_india(self) -> None:
        # 「インド太平洋」は地域概念でありインド言及でない (backfill dry-run 実測の偽陽性)
        got = nations_in_text("米陸軍、インド太平洋地域での監視強化に向け高高度気球の導入を推進")
        assert "IN" not in got
        # 本物のインド言及は引き続き拾う
        assert "IN" in nations_in_text("インドの納税インフラを標的としたサイバー攻撃")

    def test_short_iso_code_requires_exact_case(self) -> None:
        assert "US" in nations_in_text("US and Iran exchange strikes")
        assert "US" not in nations_in_text("use this tool for analysis")

    def test_english_country_name_case_insensitive(self) -> None:
        got = nations_in_text("North Korea increases illicit coal exports")
        assert "KP" in got

    def test_empty_text_returns_empty(self) -> None:
        assert nations_in_text("") == frozenset()


# ---------- assignment ----------


def _situation_row(
    *, title: str, anchors: frozenset[str], domain: str = "cyber_incident"
) -> SituationRow:
    return SituationRow(
        situation_id=situation_id_for(title),
        title=title,
        domain=domain,
        status="active",
        anchors=anchors,
        pir_ids=(),
        opened_at=_now().isoformat(),
        last_evidence_at=_now().isoformat(),
    )


class TestAssignment:
    def test_strong_anchor_shared_assigns(self) -> None:
        sit = build_situation_keys(
            _situation_row(
                title="SharePoint RCE の活発な悪用",
                anchors=frozenset({"cve:CVE-2026-1111"}),
            )
        )
        art = build_article_keys(
            article_id="a1",
            title="攻撃者が SharePoint の欠陥を悪用",
            entity_keys=frozenset({"cve:CVE-2026-1111"}),
        )
        matched = match_situation(art, [sit])
        assert matched is not None
        assert matched[1] == "anchor"

    def test_single_nation_alone_does_not_assign(self) -> None:
        # US だけの共有で何でも繋がる over-merge を防ぐ
        sit = build_situation_keys(
            _situation_row(
                title="米政府の対中輸出規制強化",
                anchors=frozenset({"involved_country:US", "involved_country:CN"}),
                domain="geopolitical",
            )
        )
        art = build_article_keys(
            article_id="a2",
            title="米海軍が新型ミサイルを試験",
            entity_keys=frozenset({"involved_country:US"}),
        )
        assert match_situation(art, [sit]) is None

    def test_nation_pair_assigns(self) -> None:
        sit = build_situation_keys(
            _situation_row(
                title="米国とイランのホルムズ海峡での応酬",
                anchors=frozenset({"involved_country:US", "involved_country:IR"}),
                domain="geopolitical",
            )
        )
        art = build_article_keys(
            article_id="a3",
            title="米国とイラン、ホルムズ海峡で攻撃を応酬",
            entity_keys=frozenset(),  # entity 未抽出でも gazetteer が US/IR を導出
        )
        matched = match_situation(art, [sit])
        assert matched is not None

    def test_best_match_prefers_strong_anchor(self) -> None:
        weak = build_situation_keys(
            _situation_row(
                title="中露の合同軍事演習",
                anchors=frozenset({"involved_country:CN", "involved_country:RU"}),
                domain="geopolitical",
            )
        )
        strong = build_situation_keys(
            _situation_row(
                title="Mustang Panda の中国発キャンペーン",
                anchors=frozenset({"actor:Mustang Panda", "involved_country:CN"}),
            )
        )
        art = build_article_keys(
            article_id="a4",
            title="Mustang Panda が中露を巡る新標的を攻撃",
            entity_keys=frozenset({"actor:Mustang Panda"}),
        )
        matched = match_situation(art, [weak, strong])
        assert matched is not None
        assert matched[0].row.situation_id == strong.row.situation_id

    def test_campaign_entity_is_strong_anchor(self) -> None:
        # 命名作戦 (campaign tag) の共有は同一事案の強い identity
        sit = build_situation_keys(
            _situation_row(
                title="Operation Endgame によるボットネット解体",
                anchors=frozenset({"campaign:Operation Endgame"}),
            )
        )
        art = build_article_keys(
            article_id="a5",
            title="続報: 摘発の第2波",
            entity_keys=frozenset({"campaign:Operation Endgame"}),
        )
        matched = match_situation(art, [sit])
        assert matched is not None
        assert matched[1] == "anchor"

    def test_mentioned_country_counts_as_nation_key(self) -> None:
        # mentioned_country (言及タグ) も involved_country と同じ国 anchor として和集合
        sit = build_situation_keys(
            _situation_row(
                title="米国とイランの応酬",
                anchors=frozenset({"involved_country:US", "involved_country:IR"}),
                domain="geopolitical",
            )
        )
        # 国 2 + 話題 token (応酬) — mentioned_country も国キーとして効く
        art = build_article_keys(
            article_id="a6",
            title="米イラン応酬の続報分析",
            entity_keys=frozenset({"mentioned_country:US", "mentioned_country:IR"}),
        )
        assert match_situation(art, [sit]) is not None
        # 話題 token ゼロ (国だけ共有) は繋がない — 高頻度国トリオの吸着防止 (2026-07-04)
        no_topic = build_article_keys(
            article_id="a7",
            title="中東地域の海運動向レポート",
            entity_keys=frozenset({"mentioned_country:US", "mentioned_country:IR"}),
        )
        assert match_situation(no_topic, [sit]) is None


# ---------- delta 分類 ----------


class TestComputeDeltaType:
    def test_first_revision_is_opened(self) -> None:
        got = compute_delta_type(
            prev_leading=None,
            prev_confidence=None,
            leading="organized_state_op",
            confidence="low",
            was_dormant=False,
        )
        assert got == "opened"

    def test_dormant_reactivation_is_reopened(self) -> None:
        got = compute_delta_type(
            prev_leading="organized_state_op",
            prev_confidence="low",
            leading="organized_state_op",
            confidence="low",
            was_dormant=True,
        )
        assert got == "reopened"

    def test_leading_change_is_hypothesis_flip(self) -> None:
        got = compute_delta_type(
            prev_leading="organized_state_op",
            prev_confidence="moderate",
            leading="opportunistic_commodity",
            confidence="moderate",
            was_dormant=False,
        )
        assert got == "hypothesis_flip"

    def test_confidence_up_is_strengthened_down_is_weakened(self) -> None:
        up = compute_delta_type(
            prev_leading="x",
            prev_confidence="low",
            leading="x",
            confidence="moderate",
            was_dormant=False,
        )
        down = compute_delta_type(
            prev_leading="x",
            prev_confidence="high",
            leading="x",
            confidence="moderate",
            was_dormant=False,
        )
        assert (up, down) == ("strengthened", "weakened")

    def test_same_judgment_is_no_change(self) -> None:
        got = compute_delta_type(
            prev_leading="x",
            prev_confidence="moderate",
            leading="x",
            confidence="moderate",
            was_dormant=False,
        )
        assert got == "no_change"


# ---------- store ----------


@pytest.fixture
def store(tmp_path: Path) -> SituationStore:
    return SituationStore(db_path=tmp_path / "ledger.db")


class TestSituationStore:
    def test_open_situation_is_idempotent_by_title(self, store: SituationStore) -> None:
        now = _now().isoformat()
        a = store.open_situation(
            title="SharePoint RCE の悪用",
            domain="cyber_incident",
            anchors=frozenset({"cve:CVE-2026-1"}),
            pir_ids=("pir_x",),
            now_iso=now,
        )
        b = store.open_situation(
            title="SharePoint RCE の悪用",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=now,
        )
        assert a.situation_id == b.situation_id
        assert len(store.load_situations()) == 1

    def test_revision_numbering_increments(self, store: SituationStore) -> None:
        now = _now().isoformat()
        sit = store.open_situation(
            title="t",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=now,
        )
        r1 = store.add_revision(_rev(sit.situation_id, delta="opened", created_at=now))
        r2 = store.add_revision(_rev(sit.situation_id, delta="no_change", created_at=now))
        assert (r1.rev, r2.rev) == (1, 2)
        latest = store.latest_revision(sit.situation_id)
        assert latest is not None
        assert latest.rev == 2

    def test_record_assignment_is_idempotent(self, store: SituationStore) -> None:
        now = _now().isoformat()
        sit = store.open_situation(
            title="t",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=now,
        )
        first = store.record_assignment(
            situation_id=sit.situation_id,
            article_id="a1",
            added_at=now,
            assigned_by="seed",
        )
        second = store.record_assignment(
            situation_id=sit.situation_id,
            article_id="a1",
            added_at=now,
            assigned_by="anchor",
        )
        assert (first, second) == (True, False)
        assert store.assigned_article_ids() == {"a1"}
        # 割当は観測のみ: 評価済み扱いにならない (中立の証拠に化けない)
        assert store.evidence_items(sit.situation_id) == []
        counts = store.evidence_state_counts([sit.situation_id])[sit.situation_id]
        assert counts == {"total": 1, "assessed": 0, "unread": 1}

    def test_record_assessment_upgrades_prior_assignment(self, store: SituationStore) -> None:
        """割当が先行した記事の ACH 評価が失われない (旧 add_evidence skip の regression)。"""
        now = _now().isoformat()
        later = "2099-01-02T00:00:00+00:00"
        sit = store.open_situation(
            title="t",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=now,
        )
        store.record_assignment(
            situation_id=sit.situation_id, article_id="a1", added_at=now, assigned_by="anchor"
        )
        inserted = store.record_assessment(
            situation_id=sit.situation_id,
            article_id="a1",
            assessed_at=later,
            polarity="supports",
            attribution_basis="vendor_confirmed",
            excerpt="ベンダが帰属を確認した",
            source_tier="research",
        )
        assert inserted is False  # 既存行の upgrade (新規挿入ではない)
        items = store.evidence_items(sit.situation_id)
        assert len(items) == 1
        assert items[0]["polarity"] == "supports"
        assert items[0]["excerpt"] == "ベンダが帰属を確認した"
        counts = store.evidence_state_counts([sit.situation_id])[sit.situation_id]
        assert counts == {"total": 1, "assessed": 1, "unread": 0}

    def test_record_assessment_inserts_when_absent(self, store: SituationStore) -> None:
        now = _now().isoformat()
        sit = store.open_situation(
            title="t",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=now,
        )
        inserted = store.record_assessment(
            situation_id=sit.situation_id,
            article_id="a2",
            assessed_at=now,
            polarity="contradicts",
            attribution_basis="govt_confirmed",
            excerpt="政府が別アクターと確認",
            source_tier="official",
        )
        assert inserted is True
        # 割当が後から来ても評価は降格しない
        assert (
            store.record_assignment(
                situation_id=sit.situation_id, article_id="a2", added_at=now, assigned_by="nation"
            )
            is False
        )
        items = store.evidence_items(sit.situation_id)
        assert items[0]["polarity"] == "contradicts"

    def test_mark_read_dequeues_without_losing_unread(self, store: SituationStore) -> None:
        """読了は read_at で刻む — revision が立っても未読はキューに残る (silent drop 根治)。"""
        now = _now().isoformat()
        sit = store.open_situation(
            title="t",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=now,
        )
        for aid in ("a1", "a2", "a3"):
            store.record_assignment(
                situation_id=sit.situation_id, article_id=aid, added_at=now, assigned_by="anchor"
            )
        # a1 のみ読了 (prompt 供給)。revision を立てても a2/a3 は未読のまま残る。
        store.mark_read(situation_id=sit.situation_id, article_ids=["a1"], read_at=now)
        store.add_revision(_rev(sit.situation_id, delta="opened", created_at=now))
        queue = store.unread_evidence()
        assert set(queue[sit.situation_id]) == {"a2", "a3"}


def _rev(situation_id: str, *, delta: str, created_at: str) -> RevisionRow:
    from typing import cast

    from src.assessment.situation_store import DeltaType

    return RevisionRow(
        situation_id=situation_id,
        rev=0,
        claim="claim",
        claim_type="ongoing_activity",
        leading_hypothesis="organized_state_op",
        confidence="low",
        confidence_basis="",
        hypotheses_json="[]",
        assumptions_json="[]",
        missing_json="[]",
        indicators_json="[]",
        implication="",
        delta_type=cast("DeltaType", delta),
        delta_note="",
        created_at=created_at,
    )


# ---------- ledger end-to-end ----------


def _seed_article(
    repo: RunHistoryRepository,
    run_id: int,
    *,
    article_id: str,
    title: str,
    importance: str = "high",
    entities: list[tuple[str, str]] | None = None,
) -> None:
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title=title,
            url=f"https://example.com/{article_id}",
            importance=importance,
            status="posted",
        )
    )
    if entities:
        repo.add_article_entities(article_id, entities)


def _judgment(
    *,
    jid: str,
    claim: str,
    domain: str,
    article_ids: list[str],
    confidence: str = "moderate",
    leading: str = "organized_state_op",
) -> KeyJudgment:
    return KeyJudgment(
        id=jid,
        claim=claim,
        domain=domain,
        leading_hypothesis=leading,
        confidence=confidence,  # type: ignore[arg-type]
        confidence_basis="test",
        hypotheses=(),
        evidence=tuple(
            EvidenceItem(
                article_id=a,
                source_tier="news",
                attribution_basis="researcher_assessed",
                excerpt="抜粋",
                polarity="supports",
            )
            for a in article_ids
        ),
    )


class TestUpdateLedger:
    def test_end_to_end_open_assign_and_second_run_no_change(self, tmp_path: Path) -> None:
        # Arrange: 判定対象 + 同一 CVE の関連記事 + 無関係 high 記事をプールに用意
        db = tmp_path / "e2e.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="test", dry_run=True))
        _seed_article(
            repo,
            run_id,
            article_id="a-seed",
            title="SharePoint RCE が悪用されている",
            entities=[("cve", "CVE-2026-9999"), ("pir", "pir_x")],
        )
        _seed_article(
            repo,
            run_id,
            article_id="a-related",
            title="SharePoint の欠陥悪用で新たな被害",
            entities=[("cve", "CVE-2026-9999")],
        )
        _seed_article(
            repo,
            run_id,
            article_id="a-unrelated",
            title="全く別の重要インシデントの報告",
        )
        est = Estimate(
            period_type="daily",
            period_start=_now(),
            period_end=_now(),
            judgments=(
                _judgment(
                    jid="j1",
                    claim="SharePoint RCE の活発な悪用が確認された",
                    domain="cyber_incident",
                    article_ids=["a-seed"],
                ),
            ),
        )

        # Act: 初回更新
        stats1 = update_ledger(est=est, repo=repo, store=store, db_path=db, now=_now())

        # Assert: 開設 + seed 証拠 + 関連記事が CVE 共有で割当、無関係 high は未割当台帳へ
        assert stats1.opened == 1
        assert stats1.revisions == 1
        assert stats1.assigned_articles >= 1
        assert stats1.unassigned_high >= 1
        situations = store.load_situations()
        assert len(situations) == 1
        evidence = store.evidence_ids_by_situation([situations[0].situation_id])
        assert {"a-seed", "a-related"} <= evidence[situations[0].situation_id]

        # Act: 同一 estimate で 2 回目 (見逃し回復の反復に相当)
        stats2 = update_ledger(est=est, repo=repo, store=store, db_path=db, now=_now())

        # Assert: 再開設しない・revision は no_change で積む
        assert stats2.opened == 0
        assert len(store.load_situations()) == 1
        latest = store.latest_revision(situations[0].situation_id)
        assert latest is not None
        assert latest.rev == 2
        assert latest.delta_type == "no_change"

    def test_confidence_rise_records_strengthened(self, tmp_path: Path) -> None:
        db = tmp_path / "delta.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="test", dry_run=True))
        _seed_article(
            repo,
            run_id,
            article_id="a1",
            title="X 社への侵害が確認された",
            entities=[("victim_org", "X 社")],
        )

        def _est(conf: str) -> Estimate:
            return Estimate(
                period_type="daily",
                period_start=_now(),
                period_end=_now(),
                judgments=(
                    _judgment(
                        jid="j1",
                        claim="X 社への侵害が拡大している",
                        domain="cyber_incident",
                        article_ids=["a1"],
                        confidence=conf,
                    ),
                ),
            )

        update_ledger(est=_est("low"), repo=repo, store=store, db_path=db, now=_now())
        update_ledger(est=_est("high"), repo=repo, store=store, db_path=db, now=_now())

        sit = store.load_situations()[0]
        latest = store.latest_revision(sit.situation_id)
        assert latest is not None
        assert latest.delta_type == "strengthened"

    def test_lifecycle_sweep_marks_dormant_and_closed(self, tmp_path: Path) -> None:
        db = tmp_path / "life.db"
        store = SituationStore(db_path=db)
        old = (_now() - timedelta(days=20)).isoformat()
        sit = store.open_situation(
            title="古い事案",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=old,
        )
        dormant, closed = _sweep_lifecycle(store=store, now=_now())
        assert dormant == 1
        row = store.get_situation(sit.situation_id)
        assert row is not None and row.status == "dormant"

        # dormancy + 90 日超で closed
        far_future = _now() + timedelta(days=120)
        dormant2, closed2 = _sweep_lifecycle(store=store, now=far_future)
        assert closed2 == 1
        row2 = store.get_situation(sit.situation_id)
        assert row2 is not None and row2.status == "closed"


def _mk_revision(sid: str, *, created_iso: str, delta: str = "opened") -> RevisionRow:
    return RevisionRow(
        situation_id=sid,
        rev=0,
        claim="テスト事案の主張",
        claim_type="ongoing_activity",
        leading_hypothesis="organized_state_op",
        confidence="moderate",
        confidence_basis="test",
        hypotheses_json="[]",
        assumptions_json="[]",
        missing_json="[]",
        indicators_json="[]",
        implication="",
        delta_type=delta,  # type: ignore[arg-type]
        delta_note="",
        created_at=created_iso,
    )


class TestUpdatePirIds:
    """L1b (2026-07-05): 取込 inline タグ遅延で空だった situation.pir_ids の自己修復。"""

    def test_update_pir_ids_roundtrip(self, tmp_path: Path) -> None:
        db = tmp_path / "pirids.db"
        store = SituationStore(db_path=db)
        now = _now().isoformat()
        sit = store.open_situation(
            title="PIR 空で開いた事案",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),  # 開設時は空 (inline タグ未到達を模擬)
            now_iso=now,
        )
        before = store.get_situation(sit.situation_id)
        assert before is not None and before.pir_ids == ()

        store.update_pir_ids(sit.situation_id, ("pir_china_apt", "pir_geopolitical_cyber"))

        row = store.get_situation(sit.situation_id)
        assert row is not None
        assert row.pir_ids == ("pir_china_apt", "pir_geopolitical_cyber")


class TestRevisionsWindowBounds:
    """監査 backlog 2026-07-05: revisions_since の period_end 上限 (backfill 混入防止)。"""

    def test_until_bound_is_inclusive_and_excludes_later(self, tmp_path: Path) -> None:
        db = tmp_path / "bounds.db"
        store = SituationStore(db_path=db)
        base = _now() - timedelta(days=10)
        sit = store.open_situation(
            title="境界テスト事案",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=base.isoformat(),
        )
        t1 = base.isoformat()
        t2 = (base + timedelta(days=3)).isoformat()
        t3 = (base + timedelta(days=6)).isoformat()
        for t in (t1, t2, t3):
            store.add_revision(_mk_revision(sit.situation_id, created_iso=t))

        got = store.revisions_since(t1, until_iso=t2)

        revs = got[sit.situation_id]
        assert [r.created_at for r in revs] == [t1, t2]  # t2 inclusive / t3 除外

    def test_latest_revision_before_skips_future(self, tmp_path: Path) -> None:
        db = tmp_path / "before.db"
        store = SituationStore(db_path=db)
        base = _now() - timedelta(days=10)
        sit = store.open_situation(
            title="未来除外事案",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=base.isoformat(),
        )
        t1 = base.isoformat()
        t2 = (base + timedelta(days=5)).isoformat()
        store.add_revision(_mk_revision(sit.situation_id, created_iso=t1))
        store.add_revision(_mk_revision(sit.situation_id, created_iso=t2, delta="strengthened"))

        got = store.latest_revision_before(
            sit.situation_id, until_iso=(base + timedelta(days=2)).isoformat()
        )

        assert got is not None
        assert got.created_at == t1


class TestLifecycleCloseAndReopen:
    """監査 backlog 2026-07-05: closing revision producer / reopened 可視化 / ゾンビ経路。"""

    def test_auto_close_creates_closing_revision(self, tmp_path: Path) -> None:
        # Arrange: 200 日前開設 + opened revision
        db = tmp_path / "close.db"
        store = SituationStore(db_path=db)
        old = (_now() - timedelta(days=200)).isoformat()
        sit = store.open_situation(
            title="収束する事案",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=old,
        )
        store.add_revision(_mk_revision(sit.situation_id, created_iso=old))

        # Act: active→dormant (idle 50d) → dormant→closed (idle 200d ≥ 14+90)
        _sweep_lifecycle(store=store, now=_now() - timedelta(days=150))
        _, closed = _sweep_lifecycle(store=store, now=_now())

        # Assert: status closed + 低確度の closing revision が残る
        assert closed == 1
        row = store.get_situation(sit.situation_id)
        assert row is not None and row.status == "closed"
        latest = store.latest_revision(sit.situation_id)
        assert latest is not None
        assert latest.delta_type == "closing"
        assert latest.confidence == "low"
        assert "活動観測されず" in latest.delta_note

    def test_evidence_assignment_marks_reopened_revision(self, tmp_path: Path) -> None:
        # Arrange: dormant な Situation + opened revision
        db = tmp_path / "reopen.db"
        store = SituationStore(db_path=db)
        old = (_now() - timedelta(days=30)).isoformat()
        sit = store.open_situation(
            title="休眠中の事案",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=old,
        )
        store.add_revision(_mk_revision(sit.situation_id, created_iso=old))
        store.set_status(sit.situation_id, "dormant")
        row = store.get_situation(sit.situation_id)
        assert row is not None and row.status == "dormant"

        # Act: 証拠割当経路の復帰可視化 (2 回目は同一 run 内重複ガード)
        seen: set[str] = set()
        _reactivate_if_dormant(
            store=store, sit_row=row, now_iso=_now().isoformat(), reopened_seen=seen
        )
        _reactivate_if_dormant(
            store=store, sit_row=row, now_iso=_now().isoformat(), reopened_seen=seen
        )

        # Assert: reopened revision がちょうど 1 つ積まれる (rev=2)
        latest = store.latest_revision(sit.situation_id)
        assert latest is not None
        assert latest.delta_type == "reopened"
        assert latest.rev == 2

    def test_active_situation_is_not_marked_reopened(self, tmp_path: Path) -> None:
        db = tmp_path / "active.db"
        store = SituationStore(db_path=db)
        now_iso = _now().isoformat()
        sit = store.open_situation(
            title="活動中の事案",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=now_iso,
        )
        store.add_revision(_mk_revision(sit.situation_id, created_iso=now_iso))
        row = store.get_situation(sit.situation_id)
        assert row is not None

        _reactivate_if_dormant(store=store, sit_row=row, now_iso=now_iso, reopened_seen=set())

        latest = store.latest_revision(sit.situation_id)
        assert latest is not None and latest.delta_type == "opened"  # 変化なし

    def test_same_title_after_close_reopens_instead_of_zombie(self, tmp_path: Path) -> None:
        """closed と同一正規化 title の判定が再出現 → reopen (不可視ゾンビ吸着の根治)。"""
        # Arrange: 開設 → close
        db = tmp_path / "zombie.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="test", dry_run=True))
        _seed_article(
            repo,
            run_id,
            article_id="a1",
            title="Volt Typhoon の米インフラ侵害",
            entities=[("actor", "Volt Typhoon")],
        )
        claim = "Volt Typhoon による米重要インフラへの事前配置が継続している"
        est = Estimate(
            period_type="daily",
            period_start=_now(),
            period_end=_now(),
            judgments=(
                _judgment(jid="j1", claim=claim, domain="cyber_incident", article_ids=["a1"]),
            ),
        )
        update_ledger(est=est, repo=repo, store=store, db_path=db, now=_now())
        sid = store.load_situations()[0].situation_id
        store.set_status(sid, "closed", closed_at=_now().isoformat())
        assert store.load_situations(("active", "dormant")) == []

        # Act: 同一 claim の判定が再出現
        update_ledger(est=est, repo=repo, store=store, db_path=db, now=_now())

        # Assert: 同一 Situation が active に復帰 (重複開設なし・closed_at クリア)
        row = store.get_situation(sid)
        assert row is not None
        assert row.status == "active"
        assert row.closed_at is None
        latest = store.latest_revision(sid)
        assert latest is not None and latest.delta_type == "reopened"
        assert len(store.load_situations(("active", "dormant", "closed"))) == 1


class TestLedgerMode:
    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SYNTHESIS_STATE", raising=False)
        assert ledger_mode() == "off"

    def test_shadow_and_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYNTHESIS_STATE", "shadow")
        assert ledger_mode() == "shadow"
        monkeypatch.setenv("SYNTHESIS_STATE", "1")
        assert ledger_mode() == "on"


class TestAddRevisionAtomicity:
    """監査 backlog 2026-07-05: add_revision の単文採番 + UNIQUE 衝突 retry。"""

    def test_sequential_revs_and_returned_rev(self, tmp_path: Path) -> None:
        db = tmp_path / "atomic.db"
        store = SituationStore(db_path=db)
        now = _now().isoformat()
        sit = store.open_situation(
            title="採番テスト",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=now,
        )
        r1 = store.add_revision(_mk_revision(sit.situation_id, created_iso=now))
        r2 = store.add_revision(
            _mk_revision(sit.situation_id, created_iso=now, delta="strengthened")
        )
        assert (r1.rev, r2.rev) == (1, 2)
        latest = store.latest_revision(sit.situation_id)
        assert latest is not None and latest.rev == 2

    def test_unique_violation_detector(self) -> None:
        import sqlite3

        from src.assessment.situation_store import _is_unique_violation

        assert _is_unique_violation(
            sqlite3.IntegrityError("UNIQUE constraint failed: situation_revisions.rev")
        )
        assert not _is_unique_violation(RuntimeError("db down"))
        assert not _is_unique_violation(sqlite3.IntegrityError("NOT NULL constraint failed"))
