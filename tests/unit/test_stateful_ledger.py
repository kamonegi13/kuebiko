"""段B (台帳駆動 synthesis) のテスト: 増分 ACH / detect-new / claim 照合 / stateful e2e。

台帳蓄積で実測した病理 3 件 (重複開設 / flip 不安定 / 国ペア over-merge) の回帰を固定する。
設計: docs/synthesis_situation_ledger_design.md §3.1-3.3。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.assessment.assignment import (
    build_article_keys,
    build_situation_keys,
    match_claim,
    match_situation,
)
from src.assessment.situation_store import SituationRow, SituationStore, situation_id_for
from src.assessment.stateful import _final_delta, build_estimate_stateful
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.synthesis.grounded.estimate import KeyJudgment
from src.synthesis.grounded.incremental import (
    PriorJudgmentView,
    _WireDetectResult,
    _WireIncEvidence,
    _WireIncHypothesis,
    _WireIncremental,
    _WireOpenClaim,
    _WireRejected,
    detect_new_claims,
    incremental_ground_and_score,
)
from src.synthesis.grounded.passes import _WireAnalysis, _WireEvidence, _WireHypothesis


def _now() -> datetime:
    return datetime.now(UTC)


class FakeLLM:
    """generate_structured を schema で分岐して返す test double (LLM 呼出なし)。"""

    def __init__(
        self,
        incremental: _WireIncremental | None = None,
        detect: _WireDetectResult | None = None,
        analysis: _WireAnalysis | None = None,
        adversarial: Any | None = None,
    ) -> None:
        self._inc = incremental
        self._det = detect
        self._ana = analysis
        self._adv = adversarial
        self.model = "fake"
        self.schemas_seen: list[type] = []  # どの schema で呼ばれたか (fast_llm ルーティング検証用)

    async def generate(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def generate_structured(self, prompt: str, schema: type, **kw: Any) -> Any:
        self.schemas_seen.append(schema)
        if schema is _WireIncremental:
            assert self._inc is not None
            return self._inc
        if schema is _WireDetectResult:
            return self._det or _WireDetectResult()
        if schema is _WireAnalysis:
            assert self._ana is not None
            return self._ana
        # adversarial (_WireReviews) はモック未指定なら空応答相当を返す
        if self._adv is not None:
            return self._adv
        return schema()


def _prior(**kw: Any) -> PriorJudgmentView:
    base: dict[str, Any] = {
        "claim": "X 社への侵害が継続している",
        "claim_type": "ongoing_activity",
        "leading_hypothesis": "organized_state_op",
        "confidence": "moderate",
        "hypotheses": ({"hypothesis": "organized_state_op", "consistent": 3, "inconsistent": 1},),
        "indicators": ("新たな被害組織の公表",),
        "key_excerpts": ({"polarity": "supports", "excerpt": "既存抜粋"},),
    }
    base.update(kw)
    return PriorJudgmentView(**base)


class TestIncrementalGroundAndScore:
    @pytest.mark.asyncio
    async def test_maps_fields_and_fired_indicators(self) -> None:
        inc = _WireIncremental(
            evidence=[
                _WireIncEvidence(
                    article_id="a1",
                    attribution_basis="vendor_confirmed",
                    excerpt="新被害の公表",
                    polarity="supports",
                )
            ],
            hypotheses=[_WireIncHypothesis(hypothesis="organized_state_op", consistent=4)],
            leading_hypothesis="organized_state_op",
            confidence="moderate",
            claim="X 社への侵害が第三国にも拡大している",
            claim_type="ongoing_activity",
            implication="日本の関連企業も注視が必要。",
            fired_indicators=["新たな被害組織の公表"],
            scope_expanded=True,
        )
        got = await incremental_ground_and_score(
            llm=FakeLLM(incremental=inc),  # type: ignore[arg-type]
            situation_title="X 社侵害",
            prior=_prior(),
            domain="cyber_incident",
            sources=[{"article_id": "a1", "feed_title": "f", "text": "本文"}],
            tier_by_id={"a1": "research"},
        )
        assert got.claim == "X 社への侵害が第三国にも拡大している"
        assert got.fired_indicators == ("新たな被害組織の公表",)
        assert got.scope_expanded is True
        assert got.analysis.evidence[0].source_tier == "research"
        assert got.analysis.implication.startswith("日本の関連企業")

    @pytest.mark.asyncio
    async def test_garbled_claim_revision_is_rejected(self) -> None:
        # 実測 (2026-07-04 本番 run): 31B が改訂 claim に簡体字/CJK拡張を混入して保存された
        from src.synthesis.grounded.incremental import is_sane_japanese_claim

        garbled = "イランの最高弔导能ンエ异席の場事が開始され、絁周国や缙国・イスラエルの新たな䆆攱"
        assert is_sane_japanese_claim(garbled) is False
        assert is_sane_japanese_claim("イランで最高指導者の葬儀が開始された") is True

        inc = _WireIncremental(leading_hypothesis="organized_state_op", claim=garbled)
        got = await incremental_ground_and_score(
            llm=FakeLLM(incremental=inc),  # type: ignore[arg-type]
            situation_title="t",
            prior=_prior(),
            domain="cyber_incident",
            sources=[{"article_id": "a1", "feed_title": "f", "text": "本文"}],
            tier_by_id={},
        )
        assert got.claim == _prior().claim  # 破損改訂は棄却され前回 claim 維持

    @pytest.mark.asyncio
    async def test_unknown_leading_falls_back_to_prior(self) -> None:
        # parse 欠落を unverified に倒すと flip ノイズになる → 前回 leading 維持
        inc = _WireIncremental(leading_hypothesis="garbled!!", claim="c")
        got = await incremental_ground_and_score(
            llm=FakeLLM(incremental=inc),  # type: ignore[arg-type]
            situation_title="t",
            prior=_prior(),
            domain="cyber_incident",
            sources=[{"article_id": "a1", "feed_title": "f", "text": "本文"}],
            tier_by_id={},
        )
        assert got.analysis.leading_hypothesis == "organized_state_op"


class TestDetectNewClaims:
    @pytest.mark.asyncio
    async def test_filters_invalid_ids_and_caps(self) -> None:
        det = _WireDetectResult(
            open=[
                _WireOpenClaim(claim=f"新情勢 {i}", domain="cyber_incident", article_ids=["a1"])
                for i in range(7)  # 上限 5 超
            ]
            + [_WireOpenClaim(claim="偽 id", domain="x", article_ids=["not-in-pool"])],
            rejected=[
                _WireRejected(article_id="a2", reason="既知事案の再報道"),
                _WireRejected(article_id="ghost", reason="無効"),
            ],
        )
        got = await detect_new_claims(
            llm=FakeLLM(detect=det),  # type: ignore[arg-type]
            articles=[
                {"article_id": "a1", "title": "t1", "feed_title": "f", "importance": "high"},
                {"article_id": "a2", "title": "t2", "feed_title": "f", "importance": "high"},
            ],
            active_titles=["既存情勢"],
            pir_context=[{"id": "p", "title": "PIR", "description": "d"}],
            period_label="L",
        )
        assert len(got.open) == 5  # cap
        assert got.overflow == 2
        assert got.rejected == (("a2", "既知事案の再報道"),)

    @pytest.mark.asyncio
    async def test_empty_input_skips_llm(self) -> None:
        got = await detect_new_claims(
            llm=FakeLLM(),  # type: ignore[arg-type]
            articles=[],
            active_titles=[],
            pir_context=[],
            period_label="L",
        )
        assert got.open == ()


class TestClaimMatching:
    def test_token_only_rule_prevents_duplicate_open(self) -> None:
        # 実測病理: NetNut 解体が言い換え claim で 2 重開設された (国も強 entity も無し)
        sit = build_situation_keys(
            SituationRow(
                situation_id=situation_id_for("Google と FBI が NetNut プロキシボットネットを解体"),
                title="Google と FBI が NetNut プロキシボットネットを解体",
                domain="cyber_incident",
                status="active",
                anchors=frozenset(),
                pir_ids=(),
                opened_at=_now().isoformat(),
                last_evidence_at=_now().isoformat(),
            )
        )
        rephrased = build_article_keys(
            article_id="",
            title="Google と FBI が 200 万台規模のボットネット NetNut を解体",
            entity_keys=frozenset(),
        )
        assert match_situation(rephrased, [sit]) is None  # 記事割当規則では届かない
        matched = match_claim(rephrased, [sit])  # claim 照合は token>=3 で届く
        assert matched is not None

    def test_discrete_event_requires_token_for_nation_pair(self) -> None:
        # 実測病理: 単発事象 (キーウ攻撃) に RU+UA の一般戦況報道が国ペアだけで吸着
        sit = build_situation_keys(
            SituationRow(
                situation_id="s-kyiv",
                title="ロシアによるキーウへの大規模なドローンおよびミサイル攻撃",
                domain="military",
                status="active",
                anchors=frozenset({"involved_country:RU", "involved_country:UA"}),
                pir_ids=(),
                opened_at=_now().isoformat(),
                last_evidence_at=_now().isoformat(),
            ),
            claim_type="discrete_event",
        )
        unrelated_war_news = build_article_keys(
            article_id="a1",
            title="ロシア軍、ウクライナ東部の集落を制圧したと発表",
            entity_keys=frozenset(),
        )
        assert match_situation(unrelated_war_news, [sit]) is None
        followup = build_article_keys(
            article_id="a2",
            title="ロシアによるキーウへのミサイル攻撃、死者数が増加",
            entity_keys=frozenset(),
        )
        # 国 1 + 話題 token 2 (キーウ/ミサイル) → 続報は繋がる
        assert match_situation(followup, [sit]) is not None


class TestDetectFastLlmRouting:
    """detect-new (入力の多い triage) は fast_llm があればそちらへ回す (2026-07-07)。"""

    @pytest.mark.asyncio
    async def test_detect_uses_fast_llm_not_main(self, tmp_path: Path) -> None:
        db = tmp_path / "r.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="t", dry_run=True))
        # Situation 皆無 → 記事は全て未割当 → detect-new が走る
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a-new",
                title="新種ワイパーが欧州の電力会社を破壊",
                url="https://x/a-new",
                importance="high",
                status="posted",
                summary="本文相当の要約テキスト。",
            )
        )
        det = _WireDetectResult(
            open=[
                _WireOpenClaim(
                    claim="新種ワイパーによる欧州電力への破壊活動",
                    domain="cyber_incident",
                    article_ids=["a-new"],
                )
            ]
        )
        slow = FakeLLM()  # 主 (31B 相当)。detect は渡さない
        fast = FakeLLM(detect=det)  # 高速 (26B 相当)
        await build_estimate_stateful(
            llm=slow,  # type: ignore[arg-type]
            fast_llm=fast,  # type: ignore[arg-type]
            period_type="daily",
            repo=repo,
            store=store,
            now=_now(),
            db_path=db,
        )
        # detect (_WireDetectResult) は fast_llm へ、slow には行かない
        assert _WireDetectResult in fast.schemas_seen
        assert _WireDetectResult not in slow.schemas_seen


class TestFinalDelta:
    def _prev(self) -> Any:
        from src.assessment.situation_store import RevisionRow

        return RevisionRow(
            situation_id="s",
            rev=1,
            claim="c",
            claim_type="ongoing_activity",
            leading_hypothesis="organized_state_op",
            confidence="moderate",
            confidence_basis="",
            hypotheses_json="[]",
            assumptions_json="[]",
            missing_json="[]",
            indicators_json="[]",
            implication="",
            delta_type="opened",
            delta_note="",
            created_at=_now().isoformat(),
        )

    def test_scope_expansion_is_escalated(self) -> None:
        got = _final_delta(
            prev=self._prev(),
            was_dormant=False,
            leading="organized_state_op",
            confidence="moderate",
            claim="c",
            scope_expanded=True,
        )
        assert got == "escalated"

    def test_claim_text_change_is_claim_revised(self) -> None:
        got = _final_delta(
            prev=self._prev(),
            was_dormant=False,
            leading="organized_state_op",
            confidence="moderate",
            claim="c (改訂)",
            scope_expanded=False,
        )
        assert got == "claim_revised"

    def test_flip_takes_priority_over_escalation(self) -> None:
        got = _final_delta(
            prev=self._prev(),
            was_dormant=False,
            leading="opportunistic_commodity",
            confidence="moderate",
            claim="c",
            scope_expanded=True,
        )
        assert got == "hypothesis_flip"


class TestBuildEstimateStateful:
    @pytest.mark.asyncio
    async def test_updates_existing_situation_and_opens_new(self, tmp_path: Path) -> None:
        # Arrange: 既存 Situation (CVE anchor + 初回 revision) + 続報記事 + 未割当 high 記事
        db = tmp_path / "b.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="t", dry_run=True))
        for aid, title, ents in [
            ("a-follow", "SharePoint 悪用の被害が拡大", [("cve", "CVE-2026-9999")]),
            ("a-new", "新種ワイパーが欧州の電力会社を破壊", [("malware_family", "NewWiper")]),
        ]:
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=aid,
                    title=title,
                    url=f"https://x/{aid}",
                    importance="high",
                    status="posted",
                    summary="本文相当の要約テキスト。",
                )
            )
            repo.add_article_entities(aid, ents)
        sit = store.open_situation(
            title="SharePoint RCE の活発な悪用",
            domain="cyber_incident",
            anchors=frozenset({"cve:CVE-2026-9999"}),
            pir_ids=(),
            now_iso=_now().isoformat(),
        )
        from tests.unit.test_situation_ledger import _rev

        store.add_revision(_rev(sit.situation_id, delta="opened", created_at=_now().isoformat()))

        inc = _WireIncremental(
            evidence=[
                _WireIncEvidence(article_id="a-follow", excerpt="被害拡大", polarity="supports")
            ],
            hypotheses=[_WireIncHypothesis(hypothesis="organized_state_op", consistent=2)],
            leading_hypothesis="organized_state_op",
            confidence="moderate",
            claim="SharePoint RCE の悪用被害が拡大している",
            claim_type="ongoing_activity",
        )
        det = _WireDetectResult(
            open=[
                _WireOpenClaim(
                    claim="新種ワイパーによる欧州電力への破壊活動",
                    domain="cyber_incident",
                    article_ids=["a-new"],
                )
            ]
        )
        ana = _WireAnalysis(
            evidence=[_WireEvidence(article_id="a-new", excerpt="破壊を確認", polarity="supports")],
            hypotheses=[_WireHypothesis(hypothesis="organized_state_op", consistent=1)],
            leading_hypothesis="organized_state_op",
            confidence="low",
            claim_type="ongoing_activity",
        )
        llm = FakeLLM(incremental=inc, detect=det, analysis=ana)

        # Act
        est = await build_estimate_stateful(
            llm=llm,  # type: ignore[arg-type]
            period_type="daily",
            repo=repo,
            store=store,
            now=_now(),
            db_path=db,
        )

        # Assert: 既存は増分更新 (rev 2)。ACH は moderate だが source_basis (弱ソース) cap で
        # low 維持 → 確度不変 + claim 文言改訂 = claim_revised (較正と delta が連動する証明)
        assert len(est.judgments) == 2
        latest = store.latest_revision(sit.situation_id)
        assert latest is not None
        assert latest.rev == 2
        assert latest.delta_type == "claim_revised"
        assert latest.claim == "SharePoint RCE の悪用被害が拡大している"
        situations = store.load_situations()
        assert len(situations) == 2  # 既存 + 新規開設

    @pytest.mark.asyncio
    async def test_quiet_day_falls_back_to_standing(self, tmp_path: Path) -> None:
        # 新着ゼロの日: 動いた判定なし → standing 上位を射影 (新規 revision は書かない)
        db = tmp_path / "q.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        sit = store.open_situation(
            title="継続中の情勢",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=_now().isoformat(),
        )
        from tests.unit.test_situation_ledger import _rev

        store.add_revision(_rev(sit.situation_id, delta="opened", created_at=_now().isoformat()))

        est = await build_estimate_stateful(
            llm=FakeLLM(),  # type: ignore[arg-type]
            period_type="daily",
            repo=repo,
            store=store,
            now=_now(),
            db_path=db,
        )
        assert len(est.judgments) == 1
        assert est.judgments[0].id == sit.situation_id
        latest = store.latest_revision(sit.situation_id)
        assert latest is not None
        assert latest.rev == 1  # fallback は revision を増やさない


class TestAnchoringGolden:
    @pytest.mark.asyncio
    async def test_refuting_evidence_flips_high_confidence_prior(self, tmp_path: Path) -> None:
        """アンカリング golden: 高確度の前回判定でも反証証拠の新着で flip すること (対称原則)。"""
        db = tmp_path / "gold.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="t", dry_run=True))
        _seed = ArticleRecord(
            run_id=run_id,
            article_id="a-refute",
            title="X 社の侵害は設定不備が原因と判明",
            url="https://x/a-refute",
            importance="high",
            status="posted",
            summary="ベンダ調査で組織的作戦の痕跡は否定された。",
        )
        repo.add_article(_seed)
        repo.add_article_entities("a-refute", [("victim_org", "X 社")])
        sit = store.open_situation(
            title="X 社への組織的侵害",
            domain="cyber_incident",
            anchors=frozenset({"victim_org:X 社"}),
            pir_ids=(),
            now_iso=_now().isoformat(),
        )
        from src.assessment.situation_store import RevisionRow as RevRow

        store.add_revision(
            RevRow(
                situation_id=sit.situation_id,
                rev=0,
                claim="X 社への組織的侵害が進行",
                claim_type="ongoing_activity",
                leading_hypothesis="organized_state_op",
                confidence="high",
                confidence_basis="",
                hypotheses_json="[]",
                assumptions_json="[]",
                missing_json="[]",
                indicators_json="[]",
                implication="",
                delta_type="opened",
                delta_note="",
                created_at=_now().isoformat(),
            )
        )
        inc = _WireIncremental(
            evidence=[
                _WireIncEvidence(
                    article_id="a-refute",
                    attribution_basis="vendor_confirmed",
                    excerpt="組織的作戦の痕跡は否定",
                    polarity="contradicts",
                )
            ],
            hypotheses=[_WireIncHypothesis(hypothesis="accidental_negligence", consistent=3)],
            leading_hypothesis="accidental_negligence",
            confidence="moderate",
            claim="X 社の侵害は設定不備由来とみられる",
            claim_type="discrete_event",
        )
        est = await build_estimate_stateful(
            llm=FakeLLM(incremental=inc),  # type: ignore[arg-type]
            period_type="daily",
            repo=repo,
            store=store,
            now=_now(),
            db_path=db,
        )
        latest = store.latest_revision(sit.situation_id)
        assert latest is not None
        assert latest.delta_type == "hypothesis_flip"
        assert latest.leading_hypothesis == "accidental_negligence"
        moved = [j for j in est.judgments if j.delta_type == "hypothesis_flip"]
        assert moved and moved[0].id == sit.situation_id


class TestStageD:
    @pytest.mark.asyncio
    async def test_weekly_estimate_uses_window_trajectory(self, tmp_path: Path) -> None:
        # weekly は「本 run で動いた判定」でなく「期間内 revision 軌跡」を判定に昇格する
        db = tmp_path / "wk.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        sit = store.open_situation(
            title="週内に動いた情勢",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=("pir_x",),
            now_iso=_now().isoformat(),
        )
        from tests.unit.test_situation_ledger import _rev

        r1 = _rev(sit.situation_id, delta="opened", created_at=_now().isoformat())
        store.add_revision(r1)
        store.add_revision(
            _rev(sit.situation_id, delta="strengthened", created_at=_now().isoformat())
        )
        est = await build_estimate_stateful(
            llm=FakeLLM(),  # type: ignore[arg-type]
            period_type="weekly",
            repo=repo,
            store=store,
            now=_now(),
            db_path=db,
        )
        j = next(x for x in est.judgments if x.id == sit.situation_id)
        assert j.delta_type == "opened"  # 期間内の最強 delta
        assert "期間内の推移" in j.delta_note
        assert j.pir_ids == ("pir_x",)

    @pytest.mark.asyncio
    async def test_relations_from_shared_actor_anchor(self, tmp_path: Path) -> None:
        db = tmp_path / "rel.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        from tests.unit.test_situation_ledger import _rev

        for title in ("Kimsuky の採用標的キャンペーン", "Kimsuky の暗号資産窃取活動"):
            s = store.open_situation(
                title=title,
                domain="cyber_incident",
                anchors=frozenset({"actor:Kimsuky"}),
                pir_ids=(),
                now_iso=_now().isoformat(),
            )
            store.add_revision(_rev(s.situation_id, delta="opened", created_at=_now().isoformat()))
        est = await build_estimate_stateful(
            llm=FakeLLM(),  # type: ignore[arg-type]
            period_type="daily",
            repo=repo,
            store=store,
            now=_now(),
            db_path=db,
        )
        assert any(r[2] == "same_actor" and r[3] == "Kimsuky" for r in est.relations)


class TestRefreshLedgerAssignments:
    def test_assignment_only_no_llm_no_revision(self, tmp_path: Path) -> None:
        # auto-trigger 用の軽量リフレッシュ: 証拠は載るが revision は増えない (評価は定時のみ)
        from src.assessment.stateful import refresh_ledger_assignments

        db = tmp_path / "rf.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="t", dry_run=True))
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a-new",
                title="SharePoint 悪用の続報",
                url="https://x/a-new",
                importance="high",
                status="posted",
            )
        )
        repo.add_article_entities("a-new", [("cve", "CVE-2026-9999")])
        sit = store.open_situation(
            title="SharePoint RCE の悪用",
            domain="cyber_incident",
            anchors=frozenset({"cve:CVE-2026-9999"}),
            pir_ids=(),
            now_iso=_now().isoformat(),
        )
        from tests.unit.test_situation_ledger import _rev

        store.add_revision(_rev(sit.situation_id, delta="opened", created_at=_now().isoformat()))

        assigned = refresh_ledger_assignments(repo=repo, store=store, db_path=db, now=_now())

        assert assigned == 1
        assert "a-new" in store.evidence_ids_by_situation([sit.situation_id])[sit.situation_id]
        latest = store.latest_revision(sit.situation_id)
        assert latest is not None
        assert latest.rev == 1  # revision は増えない (LLM 評価なし)


class TestRehydrateForProjection:
    """監査 2026-07-05 P3: standing/trajectory 射影の証拠再水和。

    _revision_to_judgment は evidence=() で復元するため weekly/monthly の旗艦報告が
    article_count=0 + 虚偽 source_caveat「接地証拠が乏しく」を出し、japan_related=False
    固定で W_JP boost が weekly headline 選定で死んでいた病理の回帰固定。
    """

    def _judgment(self, sid: str = "s-1") -> KeyJudgment:
        return KeyJudgment(
            id=sid,
            claim="日本の防衛関連企業への侵入活動",
            domain="cyber_incident",
            leading_hypothesis="organized_state_op",
            confidence="moderate",
            confidence_basis="",
            hypotheses=(),
            evidence=(),
        )

    def test_rehydrates_evidence_and_japan_flag(self) -> None:
        from src.assessment.stateful import _rehydrate_for_projection

        class FakeStore:
            def evidence_items(self, sid: str, *, limit: int = 5) -> list[dict[str, str]]:
                return [
                    {
                        "article_id": "a1",
                        "polarity": "supports",
                        "attribution_basis": "vendor_confirmed",
                        "excerpt": "抜粋",
                        "source_tier": "research",
                    }
                ]

            def evidence_state_counts(self, sids: list[str]) -> dict[str, dict[str, int]]:
                # 評価済み 1 + 割当のみ 2 → unassessed_count=2 が判定に載る
                return {sid: {"total": 3, "assessed": 1, "unread": 2} for sid in sids}

            def evidence_ids_by_situation(self, sids: list[str]) -> dict[str, set[str]]:
                # entity 幅は割当記事も含む (a2 は未評価だが JP entity の観測に使える)
                return {sid: {"a1", "a2"} for sid in sids}

        class FakeRepo:
            def entity_keys_for_articles(self, ids: list[str]) -> dict[str, set[str]]:
                return {"a1": {"involved_country:JP"}}

        out = _rehydrate_for_projection(
            [self._judgment()],
            store=FakeStore(),  # type: ignore[arg-type]
            repo=FakeRepo(),  # type: ignore[arg-type]
        )
        assert len(out[0].evidence) == 1
        assert out[0].evidence[0].article_id == "a1"
        assert out[0].japan_related is True
        assert out[0].unassessed_count == 2

    def test_rehydrate_failure_keeps_judgment(self) -> None:
        from src.assessment.stateful import _rehydrate_for_projection

        class BrokenStore:
            def evidence_items(self, sid: str, *, limit: int = 5) -> list[dict[str, str]]:
                raise RuntimeError("db down")

        out = _rehydrate_for_projection(
            [self._judgment()],
            store=BrokenStore(),  # type: ignore[arg-type]
            repo=None,  # type: ignore[arg-type]
        )
        assert out[0].claim == "日本の防衛関連企業への侵入活動"
        assert out[0].evidence == ()


class TestClosingProjection:
    """監査 backlog 2026-07-05: 期間内 close の「収束」射影 (_closing_judgments)。"""

    @staticmethod
    def _setup_closed(
        tmp_path: Path, *, closed_at_iso: str
    ) -> tuple[SituationStore, RunHistoryRepository, str]:
        from datetime import timedelta

        from src.assessment.situation_store import RevisionRow

        db = tmp_path / "closing.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        old = (_now() - timedelta(days=120)).isoformat()
        sit = store.open_situation(
            title="収束した事案",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=("pir_x",),
            now_iso=old,
        )
        store.add_revision(
            RevisionRow(
                situation_id=sit.situation_id,
                rev=0,
                claim="収束した事案の主張",
                claim_type="ongoing_activity",
                leading_hypothesis="organized_state_op",
                confidence="low",
                confidence_basis="自動収束",
                hypotheses_json="[]",
                assumptions_json="[]",
                missing_json="[]",
                indicators_json="[]",
                implication="",
                delta_type="closing",
                delta_note="活動観測されず",
                created_at=closed_at_iso,
            )
        )
        store.set_status(sit.situation_id, "closed", closed_at=closed_at_iso)
        return store, repo, sit.situation_id

    def test_recent_close_is_projected_as_closing(self, tmp_path: Path) -> None:
        from datetime import timedelta

        from src.assessment.stateful import _closing_judgments

        closed_at = _now().isoformat()
        store, repo, sid = self._setup_closed(tmp_path, closed_at_iso=closed_at)

        out = _closing_judgments(
            store=store,
            repo=repo,
            start_iso=(_now() - timedelta(hours=24)).isoformat(),
            moved_ids=set(),
        )

        assert [j.id for j in out] == [sid]
        assert out[0].delta_type == "closing"
        assert out[0].pir_ids == ("pir_x",)

    def test_old_close_is_not_projected(self, tmp_path: Path) -> None:
        from datetime import timedelta

        from src.assessment.stateful import _closing_judgments

        closed_at = (_now() - timedelta(days=10)).isoformat()
        store, repo, _sid = self._setup_closed(tmp_path, closed_at_iso=closed_at)

        out = _closing_judgments(
            store=store,
            repo=repo,
            start_iso=(_now() - timedelta(hours=24)).isoformat(),
            moved_ids=set(),
        )

        assert out == []


class TestReassessmentQueue:
    """P1 (再評価飢餓) の回帰固定: 毎時 refresh が先に割当てた証拠を定時 run が必ず評価する。

    2026-07-11 実測: 収集イベント=割当のみ (64e5b690) 化以降、synthesis は「未割当記事」
    しか増分 ACH に回さず、割当済み証拠が誰にも評価されない飢餓が発生 (33/63 Situation)。
    """

    def test_unread_evidence_excludes_read_articles(self, tmp_path: Path) -> None:
        # Arrange: 開設 run が prompt に読ませた証拠 (mark_read 済) + それ以後の割当
        from tests.unit.test_situation_ledger import _rev

        db = tmp_path / "u.db"
        store = SituationStore(db_path=db)
        t0 = "2026-07-10T00:00:00+00:00"
        t1 = "2026-07-10T01:00:00+00:00"
        sit = store.open_situation(
            title="X 社への侵害",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=t0,
        )
        store.record_assignment(
            situation_id=sit.situation_id, article_id="a0", added_at=t0, assigned_by="seed"
        )
        store.mark_read(situation_id=sit.situation_id, article_ids=["a0"], read_at=t0)
        store.add_revision(_rev(sit.situation_id, delta="opened", created_at=t0))
        store.record_assignment(
            situation_id=sit.situation_id, article_id="a1", added_at=t1, assigned_by="anchor"
        )

        # Act / Assert: 読了済みは含まれず、未読の割当だけが返る
        assert store.unread_evidence() == {sit.situation_id: ["a1"]}

    def test_unread_evidence_survives_revision(self, tmp_path: Path) -> None:
        """未読は revision が立っても脱落しない (旧 added_at 比較の silent drop 根治)。"""
        from tests.unit.test_situation_ledger import _rev

        db = tmp_path / "u3.db"
        store = SituationStore(db_path=db)
        t0 = "2026-07-10T00:00:00+00:00"
        t1 = "2026-07-10T02:00:00+00:00"
        sit = store.open_situation(
            title="読まれずに revision が立つ",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=t0,
        )
        store.record_assignment(
            situation_id=sit.situation_id, article_id="a0", added_at=t0, assigned_by="anchor"
        )
        # 読まないまま revision が立つ (他の記事で判定が動いた等) — a0 はキューに残る
        store.add_revision(_rev(sit.situation_id, delta="opened", created_at=t1))
        assert store.unread_evidence() == {sit.situation_id: ["a0"]}

    def test_unread_evidence_without_revision_returns_all(self, tmp_path: Path) -> None:
        db = tmp_path / "u2.db"
        store = SituationStore(db_path=db)
        t0 = "2026-07-10T00:00:00+00:00"
        sit = store.open_situation(
            title="revision の無い異常系",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=t0,
        )
        store.record_assignment(
            situation_id=sit.situation_id, article_id="a0", added_at=t0, assigned_by="nation"
        )
        assert store.unread_evidence() == {sit.situation_id: ["a0"]}

    def test_select_reassessments_priority_and_cap(self) -> None:
        from src.assessment.stateful import select_reassessments

        # 優先度 = 新着証拠が多い順 → 最終判定が古い順 → sid。cap 超過は繰越し。
        candidates = {"s1": ["a"], "s2": ["a", "b", "c"], "s3": ["a", "b"], "s4": ["a"]}
        latest_rev_at = {
            "s1": "2026-07-01T00:00:00+00:00",
            "s2": "2026-07-05T00:00:00+00:00",
            "s3": "2026-07-05T00:00:00+00:00",
            "s4": "2026-07-03T00:00:00+00:00",
        }
        selected, deferred = select_reassessments(candidates, latest_rev_at, cap=3)
        assert selected == ["s2", "s3", "s1"]
        assert deferred == ["s4"]

    @pytest.mark.asyncio
    async def test_hourly_assigned_evidence_is_reassessed(self, tmp_path: Path) -> None:
        # Arrange: 既存 Situation + 続報記事。毎時 refresh が**先に**証拠として割当てる
        from src.assessment.stateful import refresh_ledger_assignments
        from tests.unit.test_situation_ledger import _rev

        db = tmp_path / "r.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="t", dry_run=True))
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a-follow",
                title="SharePoint 悪用の被害が拡大",
                url="https://x/a-follow",
                importance="high",
                status="posted",
                summary="本文相当の要約テキスト。",
            )
        )
        repo.add_article_entities("a-follow", [("cve", "CVE-2026-9999")])
        sit = store.open_situation(
            title="SharePoint RCE の活発な悪用",
            domain="cyber_incident",
            anchors=frozenset({"cve:CVE-2026-9999"}),
            pir_ids=(),
            now_iso=_now().isoformat(),
        )
        store.add_revision(_rev(sit.situation_id, delta="opened", created_at=_now().isoformat()))
        assert refresh_ledger_assignments(repo=repo, store=store, db_path=db, now=_now()) == 1

        inc = _WireIncremental(
            evidence=[
                _WireIncEvidence(article_id="a-follow", excerpt="被害拡大", polarity="supports")
            ],
            hypotheses=[_WireIncHypothesis(hypothesis="organized_state_op", consistent=2)],
            leading_hypothesis="organized_state_op",
            confidence="moderate",
            claim="SharePoint RCE の悪用被害が拡大している",
            claim_type="ongoing_activity",
        )
        llm = FakeLLM(incremental=inc)

        # Act: 定時 run (割当済み証拠しか無い状態)
        est = await build_estimate_stateful(
            llm=llm,  # type: ignore[arg-type]
            period_type="daily",
            repo=repo,
            store=store,
            now=_now(),
            db_path=db,
        )

        # Assert: 割当済み証拠が増分 ACH で評価され revision が進む (旧実装は rev 1 のまま)
        latest = store.latest_revision(sit.situation_id)
        assert latest is not None
        assert latest.rev == 2
        assert latest.delta_type == "claim_revised"
        assert len(est.judgments) == 1
        # P2: 評価済み claim に title が追従する (単発事象 title の固定化を防ぐ)
        row = store.get_situation(sit.situation_id)
        assert row is not None
        assert row.title == "SharePoint RCE の悪用被害が拡大している"

    @pytest.mark.asyncio
    async def test_reassess_budget_defers_low_priority(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # cap=1 で 2 Situation に未評価証拠 → 判定が古い方だけ評価、他方は繰越し (revision 不変)
        from src.assessment import stateful as stateful_mod
        from src.assessment.stateful import refresh_ledger_assignments
        from tests.unit.test_situation_ledger import _rev

        monkeypatch.setitem(stateful_mod._MAX_UPDATES_BY_PERIOD, "daily", 1)
        db = tmp_path / "d.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="t", dry_run=True))
        for aid, cve in [("a-1", "CVE-2026-1111"), ("a-2", "CVE-2026-2222")]:
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=aid,
                    title=f"{cve} の悪用が拡大",
                    url=f"https://x/{aid}",
                    importance="high",
                    status="posted",
                    summary="本文相当の要約テキスト。",
                )
            )
            repo.add_article_entities(aid, [("cve", cve)])
        old = store.open_situation(
            title="古い判定の Situation",
            domain="cyber_incident",
            anchors=frozenset({"cve:CVE-2026-1111"}),
            pir_ids=(),
            now_iso=_now().isoformat(),
        )
        store.add_revision(
            _rev(old.situation_id, delta="opened", created_at="2026-07-01T00:00:00+00:00")
        )
        newer = store.open_situation(
            title="新しい判定の Situation",
            domain="cyber_incident",
            anchors=frozenset({"cve:CVE-2026-2222"}),
            pir_ids=(),
            now_iso=_now().isoformat(),
        )
        store.add_revision(_rev(newer.situation_id, delta="opened", created_at=_now().isoformat()))
        assert refresh_ledger_assignments(repo=repo, store=store, db_path=db, now=_now()) == 2

        inc = _WireIncremental(
            evidence=[_WireIncEvidence(article_id="a-1", excerpt="拡大", polarity="supports")],
            hypotheses=[_WireIncHypothesis(hypothesis="organized_state_op", consistent=2)],
            leading_hypothesis="organized_state_op",
            confidence="moderate",
            claim="",
            claim_type="ongoing_activity",
        )
        await build_estimate_stateful(
            llm=FakeLLM(incremental=inc),  # type: ignore[arg-type]
            period_type="daily",
            repo=repo,
            store=store,
            now=_now(),
            db_path=db,
        )

        # 証拠数同数 → 判定が古い old が選ばれ rev 2、newer は繰越しで rev 1 のまま
        latest_old = store.latest_revision(old.situation_id)
        latest_newer = store.latest_revision(newer.situation_id)
        assert latest_old is not None and latest_old.rev == 2
        assert latest_newer is not None and latest_newer.rev == 1


class TestSweepTargetSelection:
    """週次反証 sweep の有界ローテ (監査 2026-07-16: 出力切断バグの根治)。"""

    @staticmethod
    def _mk(sid: str, confidence: str = "low") -> KeyJudgment:
        return KeyJudgment(
            id=sid,
            claim=f"claim {sid}",
            domain="cyber_incident",
            leading_hypothesis="organized_state_op",
            confidence=confidence,  # type: ignore[arg-type]
            confidence_basis="",
            hypotheses=(),
            evidence=(),
        )

    def test_under_cap_returns_all(self) -> None:
        from src.assessment.stateful import _select_sweep_targets

        cands = [self._mk(f"s-{i:03d}") for i in range(10)]
        selected, skipped = _select_sweep_targets(cands, week_number=29)
        assert len(selected) == 10
        assert skipped == 0

    def test_over_cap_is_bounded_and_logs_skipped(self) -> None:
        from src.assessment.stateful import _SWEEP_CAP, _select_sweep_targets

        cands = [self._mk(f"s-{i:03d}") for i in range(93)]
        selected, skipped = _select_sweep_targets(cands, week_number=29)
        assert len(selected) <= _SWEEP_CAP
        assert skipped == 93 - len(selected)

    def test_rotation_covers_all_over_weeks(self) -> None:
        # Arrange: 93 件は数週のラウンドロビンで全量が一巡する (silent 永久見送りなし)
        from src.assessment.stateful import _select_sweep_targets

        cands = [self._mk(f"s-{i:03d}") for i in range(93)]
        seen: set[str] = set()
        for week in range(8):
            selected, _ = _select_sweep_targets(cands, week_number=week)
            seen |= {j.id for j in selected}
        assert seen == {f"s-{i:03d}" for i in range(93)}

    def test_deterministic_for_same_week(self) -> None:
        from src.assessment.stateful import _select_sweep_targets

        cands = [self._mk(f"s-{i:03d}") for i in range(50)]
        a, _ = _select_sweep_targets(cands, week_number=3)
        b, _ = _select_sweep_targets(cands, week_number=3)
        assert [j.id for j in a] == [j.id for j in b]
