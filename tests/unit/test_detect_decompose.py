"""detect を既存の部品へ分けた形 (M2、DETECT_DECOMPOSE) と、束ねの記録 (detect_bundles) の試験。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.assessment import detect_decompose, detect_step, stateful
from src.assessment.detect_bundle_log import (
    ORIGIN_DROPPED,
    ORIGIN_LLM,
    ORIGIN_UNBUNDLED,
    ORIGIN_UNION,
    BundleRecord,
    build_current_records,
    make_claim_id,
    resolve_outcomes,
)
from src.assessment.detect_decompose import (
    ClusterClaimWire,
    ClusterOutcome,
    DecomposeInputs,
    DecomposeResult,
    MergeVerdict,
    WrittenClaim,
    decompose_candidates,
    decompose_mode,
    merged_assignments,
    pick_merge,
    run_decompose,
    to_detect_result,
    to_records,
    ungrounded_terms,
    write_cluster_claim,
)
from src.assessment.situation_store import SituationStore
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.synthesis.grounded.incremental import (
    DetectedClaim,
    DetectResult,
    _WireDetectResult,
    _WireIncremental,
    _WireOpenClaim,
)
from src.synthesis.grounded.passes import _WireAnalysis, _WireEvidence, _WireHypothesis
from tests.unit.test_stateful_ledger import FakeLLM

_RUN_AT = "2026-10-16T06:30:00+00:00"


def _dr(*claims: tuple[str, tuple[str, ...]]) -> DetectResult:
    return DetectResult(
        open=tuple(
            DetectedClaim(claim=c, domain="cyber_incident", article_ids=ids) for c, ids in claims
        ),
        rejected=(),
        overflow=0,
    )


class TestMode:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            (None, "0"),
            ("0", "0"),
            ("shadow", "shadow"),
            ("1", "1"),
            ("SHADOW", "shadow"),
            ("on", "0"),
        ],
    )
    def test_defaults_to_off_and_unknown_values_fall_back_to_off(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None, want: str
    ) -> None:
        if raw is None:
            monkeypatch.delenv("DETECT_DECOMPOSE", raising=False)
        else:
            monkeypatch.setenv("DETECT_DECOMPOSE", raw)

        assert decompose_mode() == want


class TestShadowCaps:
    def test_shadow_claim_cap_matches_production_open_cap(self) -> None:
        from src.assessment.detect_decompose import SHADOW_MAX_CLAIMS
        from src.synthesis.grounded.incremental import _DETECT_OPEN_MAX

        assert SHADOW_MAX_CLAIMS == _DETECT_OPEN_MAX

    def test_merge_scoring_has_no_situation_cap(self) -> None:
        assert not hasattr(detect_decompose, "MAX_MERGE_SITUATIONS")


class TestPickMerge:
    def test_merges_when_more_than_half_of_the_cluster_passes(self) -> None:
        scores = {("s1", "a"): (True, 0.9), ("s1", "b"): (True, 0.7), ("s1", "c"): (False, 0.1)}

        got = pick_merge(["a", "b", "c"], scores, ["s1"])

        assert got is not None
        assert got.situation_id == "s1"
        assert got.pass_fraction == pytest.approx(2 / 3)
        assert got.probability == pytest.approx(0.8)

    def test_exactly_half_does_not_merge(self) -> None:
        scores = {("s1", "a"): (True, 0.9), ("s1", "b"): (False, 0.1)}

        assert pick_merge(["a", "b"], scores, ["s1"]) is None

    def test_unscored_article_blocks_the_situation(self) -> None:
        scores = {("s1", "a"): (True, 0.9)}

        assert pick_merge(["a", "b"], scores, ["s1"]) is None

    def test_best_fraction_then_probability_then_id_wins(self) -> None:
        scores = {
            ("s2", "a"): (True, 0.6),
            ("s1", "a"): (True, 0.6),
            ("s3", "a"): (True, 0.8),
        }

        got = pick_merge(["a"], scores, ["s2", "s1", "s3"])

        assert got is not None
        assert got.situation_id == "s3"

    def test_tie_goes_to_the_smaller_situation_id(self) -> None:
        scores = {("s2", "a"): (True, 0.6), ("s1", "a"): (True, 0.6)}

        got = pick_merge(["a"], scores, ["s2", "s1"])

        assert got is not None
        assert got.situation_id == "s1"

    def test_embed_fallback_without_probability_still_merges(self) -> None:
        got = pick_merge(["a"], {("s1", "a"): (True, None)}, ["s1"])

        assert got == MergeVerdict(situation_id="s1", pass_fraction=1.0, probability=None)


def _writer(written: dict[tuple[str, ...], WrittenClaim | None]) -> Any:
    calls: list[tuple[str, ...]] = []

    async def write(cluster: Any) -> WrittenClaim | None:
        calls.append(tuple(cluster))
        return written[tuple(cluster)]

    write.calls = calls  # type: ignore[attr-defined]
    return write


class TestDecomposeCandidates:
    @pytest.mark.asyncio
    async def test_clusters_by_event_merges_then_writes_only_the_rest(self) -> None:
        # a1+a2 = 同じ事象 (合流)、a3 = 単独 (claim を書く)、a4 = 材料なし (単独扱い)
        write = _writer(
            {
                ("a3",): WrittenClaim("Aが被害", "cyber_incident", ""),
                ("a4",): WrittenClaim("Bが被害", "weird", ""),
            }
        )

        res = await decompose_candidates(
            candidate_ids=["a1", "a2", "a3", "a4"],
            ml_scores={"a1": 0.9, "a2": 0.8, "a3": 0.7, "a4": 0.6},
            strong_by_aid={"a3": {"actor:x"}},
            items_by_aid={"a1": {"e1"}, "a2": {"e1"}},
            vec_by_aid=None,
            merge_for=lambda c: MergeVerdict("s1", 1.0, 0.9) if "a1" in c else None,
            write_claim=write,
        )

        assert [(o.action, o.cluster) for o in res.outcomes] == [
            ("merge", ("a1", "a2")),
            ("open", ("a3",)),
            ("open", ("a4",)),
        ]
        assert write.calls == [("a3",), ("a4",)]  # 合流した塊には LLM を呼ばない
        assert res.opened()[1].domain == "unclassified"

    @pytest.mark.asyncio
    async def test_writer_budget_caps_claims_and_counts_overflow(self) -> None:
        write = _writer({(a,): WrittenClaim(f"claim {a}", "cyber_incident", "") for a in "abc"})

        res = await decompose_candidates(
            candidate_ids=["a", "b", "c"],
            ml_scores={"a": 0.9, "b": 0.8, "c": 0.7},
            strong_by_aid={},
            items_by_aid={},
            vec_by_aid=None,
            merge_for=lambda c: None,
            write_claim=write,
            max_claims=2,
        )

        assert len(write.calls) == 2
        assert res.overflow == 1
        assert res.outcomes[-1].action == "skip"
        assert res.outcomes[-1].reason == "開設の上限"

    @pytest.mark.asyncio
    async def test_writer_skip_and_failure_do_not_open(self) -> None:
        async def write(cluster: Any) -> WrittenClaim | None:
            if cluster[0] == "a":
                return WrittenClaim("", "", "追跡中と同一")
            raise RuntimeError("boom")

        res = await decompose_candidates(
            candidate_ids=["a", "b"],
            ml_scores={"a": 0.9, "b": 0.8},
            strong_by_aid={},
            items_by_aid={},
            vec_by_aid=None,
            merge_for=lambda c: None,
            write_claim=write,
        )

        assert [o.action for o in res.outcomes] == ["skip", "skip"]
        assert res.outcomes[0].reason == "追跡中と同一"
        assert res.outcomes[1].reason == "claim の生成に失敗"


class TestConversions:
    def _result(self) -> DecomposeResult:
        return DecomposeResult(
            outcomes=(
                ClusterOutcome(("a", "b"), "open", claim="A社が被害", domain="cyber_incident"),
                ClusterOutcome(("c",), "merge", situation_id="s9", probability=0.8, reason="r"),
                ClusterOutcome(("d",), "skip", reason="評価対象なし"),
            ),
            overflow=0,
        )

    def test_to_detect_result_cites_exactly_the_cluster_ids(self) -> None:
        got = to_detect_result(self._result())

        assert got.open == (DetectedClaim("A社が被害", "cyber_incident", ("a", "b")),)
        assert got.rejected == (("d", "評価対象なし"),)

    def test_shadow_records_are_final_but_live_open_is_pending(self) -> None:
        shadow = to_records(self._result(), run_at=_RUN_AT, variant="decompose_shadow")
        live = to_records(self._result(), run_at=_RUN_AT, variant="decompose")

        assert [r.outcome for r in shadow] == ["shadow_open", "merged", "skipped"]
        assert [r.outcome for r in live] == ["pending", "merged", "skipped"]
        assert shadow[1].situation_id == "s9"
        assert shadow[0].final_article_ids == ("a", "b")
        assert shadow[0].llm_article_ids == ()

    def test_claim_wire_accepts_a_slightly_long_claim_but_stops_runaways(self) -> None:
        assert len(ClusterClaimWire(claim="あ" * 201).claim) == 201
        # 2026-10-18: 超過は検証失敗ではなく後処理で 600 字に収める (塊を失わない)
        assert len(ClusterClaimWire(claim="あ" * 601).claim) <= 600

    def test_claim_wire_ignores_extra_fields(self) -> None:
        got = ClusterClaimWire.model_validate({"claim": "x", "article_ids": ["bogus"]})

        assert got.claim == "x"


class TestBundleLog:
    def test_current_records_link_final_claims_to_the_llm_bundle(self) -> None:
        raw = _dr(("束ね", ("a", "b", "c")), ("消える", ("z",)))
        union = _dr(("束ね", ("a", "b", "c")), ("消える", ("z",)), ("ML", ("m",)))
        final = _dr(("A", ("a", "b")), ("C", ("c",)), ("ML", ("m",)))

        recs = build_current_records(run_at=_RUN_AT, raw_llm=raw, after_union=union, final=final)

        by_claim = {r.claim: r for r in recs}
        assert by_claim["A"].llm_article_ids == ("a", "b", "c")
        assert by_claim["A"].final_article_ids == ("a", "b")
        assert by_claim["A"].origin == ORIGIN_UNBUNDLED
        assert by_claim["C"].origin == ORIGIN_UNBUNDLED
        assert by_claim["ML"].origin == ORIGIN_UNION
        assert by_claim["ML"].llm_article_ids == ()
        assert by_claim["消える"].origin == ORIGIN_DROPPED
        assert by_claim["消える"].outcome == "skipped"

    def test_unchanged_llm_claim_has_llm_origin(self) -> None:
        raw = _dr(("X", ("a",)))

        recs = build_current_records(run_at=_RUN_AT, raw_llm=raw, after_union=raw, final=raw)

        assert [r.origin for r in recs] == [ORIGIN_LLM]

    def test_claim_id_is_stable_and_depends_on_ids_and_claim(self) -> None:
        a = make_claim_id(_RUN_AT, "current", ["a", "b"], "x")

        assert a == make_claim_id(_RUN_AT, "current", ["b", "a"], "x")
        assert a != make_claim_id(_RUN_AT, "current", ["a"], "x")
        assert a != make_claim_id(_RUN_AT, "decompose", ["a", "b"], "x")

    def test_resolve_outcomes_fills_pending_and_leaves_decided_rows(self) -> None:
        pending = BundleRecord(_RUN_AT, "current", "c1", "llm", "x", "d", (), ("a",))
        untouched = BundleRecord(_RUN_AT, "current", "c2", "llm", "y", "d", (), ("b",))
        done = pending.with_outcome("merged", situation_id="s", reason="既存")
        done = BundleRecord(**{**done.__dict__, "claim_id": "c3"})

        got = resolve_outcomes([pending, untouched, done], {"c1": ("opened", "sit-1", "")})

        assert [(r.outcome, r.situation_id) for r in got] == [
            ("opened", "sit-1"),
            ("skipped", None),
            ("merged", "s"),
        ]
        assert pending.outcome == "pending"  # 元は書き換えない

    def test_store_round_trip_in_sqlite(self, tmp_path: Path) -> None:
        db = tmp_path / "b.db"
        RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        rec = BundleRecord(
            _RUN_AT,
            "current",
            "c1",
            "llm",
            "claim",
            "cyber_incident",
            ("a", "b"),
            ("a",),
            outcome="opened",
            situation_id="s1",
            probability=0.5,
            model="m",
        )

        store.record_detect_bundles([rec])
        store.record_detect_bundles([])

        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT run_date, variant, claim_id, llm_article_ids, final_article_ids, outcome,"
                " situation_id, probability, model FROM detect_bundles"
            ).fetchall()
        assert rows == [
            ("2026-10-16", "current", "c1", '["a", "b"]', '["a"]', "opened", "s1", 0.5, "m")
        ]
        assert store.purge_detect_bundles(before_iso="2026-10-17") == 1


class TestMergedAssignments:
    def test_groups_merged_clusters_by_situation_without_touching_the_db(self) -> None:
        res = DecomposeResult(
            outcomes=(
                ClusterOutcome(("a", "b"), "merge", situation_id="s1"),
                ClusterOutcome(("c",), "merge", situation_id="s1"),
                ClusterOutcome(("d",), "open", claim="x"),
            ),
            overflow=0,
        )

        assert merged_assignments(res) == {"s1": ["a", "b", "c"]}


async def _write_invented(cluster: Any) -> WrittenClaim | None:
    return WrittenClaim("APT29 が侵入", "cyber_incident", "")


async def _decompose_one(*, enforce: bool) -> DecomposeResult:
    return await decompose_candidates(
        candidate_ids=["a"],
        ml_scores={"a": 1.0},
        strong_by_aid={},
        items_by_aid={},
        vec_by_aid=None,
        merge_for=lambda c: None,
        write_claim=_write_invented,
        grounding_text=lambda c: "ある銀行で障害",
        enforce_grounding=enforce,
    )


class TestGrounding:
    def test_flags_proper_nouns_missing_from_the_headlines(self) -> None:
        got = ungrounded_terms(
            "LockBit が CVE-2026-1234 を悪用しワイパーを展開", "lockbit が cve-2026-1234 を悪用"
        )

        assert got == ("ワイパー",)

    def test_lowercase_words_and_kanji_are_not_checked(self) -> None:
        assert ungrounded_terms("a campaign hit the bank 銀行", "") == ()

    @pytest.mark.asyncio
    async def test_enforced_gate_skips_a_claim_with_invented_names(self) -> None:
        res = await _decompose_one(enforce=True)

        assert res.outcomes[0].action == "skip"
        assert "APT29" in res.outcomes[0].reason

    @pytest.mark.asyncio
    async def test_shadow_keeps_the_claim_but_records_the_violation(self) -> None:
        res = await _decompose_one(enforce=False)

        rec = to_records(res, run_at=_RUN_AT, variant="decompose_shadow")[0]
        assert rec.outcome == "shadow_open"
        assert "違反 1 件" in rec.reason
        assert "APT29" in rec.reason


class _LongClaimLLM:
    model = "fake"

    async def generate_structured(self, prompt: str, schema: type, **kw: Any) -> Any:
        return ClusterClaimWire(claim="あ" * 300, domain="cyber_incident")


class TestWriter:
    @pytest.mark.asyncio
    async def test_overlong_claim_is_truncated_not_lost(self) -> None:
        got = await write_cluster_claim(
            llm=_LongClaimLLM(),  # type: ignore[arg-type]
            articles=[{"article_id": "a", "title": "t", "feed_title": "f", "importance": "high"}],
            active_titles=[],
            pir_context=[],
            period_label="p",
        )

        assert got is not None
        assert len(got.claim) == detect_decompose.CLAIM_MAX_CHARS

    @pytest.mark.asyncio
    async def test_persist_false_is_forwarded_to_the_scorer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        db = tmp_path / "p.db"
        repo = RunHistoryRepository(db_path=db)
        seen: dict[str, Any] = {}

        async def fake_pairs(**kw: Any) -> tuple[dict[Any, Any], list[str]]:
            seen.update(kw)
            return {}, []

        monkeypatch.setattr(detect_decompose, "score_merge_pairs", fake_pairs)
        inp = DecomposeInputs(
            candidates=[{"article_id": "a", "title": "t"}],
            ml_scores={"a": 1.0},
            entities_by_id={},
            situation_keys=[],
            art_keys={},
            repo=repo,
            store=SituationStore(db_path=db),
            llm=_LongClaimLLM(),  # type: ignore[arg-type]
            active_titles=[],
            pir_context=[],
            period_label="p",
        )

        await run_decompose(inp, merge_enabled=True, persist=False, enforce_grounding=False)

        assert seen["save_fresh"] is False


# ---- build_estimate_stateful への配線 (ML 採点・埋込は差し替え、LLM は FakeLLM) ----


class _DecomposeLLM(FakeLLM):
    async def generate_structured(self, prompt: str, schema: type, **kw: Any) -> Any:
        if schema is ClusterClaimWire:
            self.schemas_seen.append(schema)
            return ClusterClaimWire(
                claim="新種ワイパーが欧州の電力会社を破壊した", domain="cyber_incident"
            )
        return await super().generate_structured(prompt, schema, **kw)


def _seed(tmp_path: Path) -> tuple[Path, RunHistoryRepository, SituationStore]:
    db = tmp_path / "w.db"
    repo = RunHistoryRepository(db_path=db)
    store = SituationStore(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
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
    repo.add_article_entities("a-new", [("malware_family", "NewWiper")])
    return db, repo, store


def _llm(cls: type[FakeLLM] = FakeLLM) -> FakeLLM:
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
    return cls(incremental=_WireIncremental(), detect=det, analysis=ana)


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str | None,
    llm: FakeLLM,
    *,
    with_scores: bool = True,
) -> tuple[SituationStore, Path]:
    db, repo, store = _seed(tmp_path)
    if mode is None:
        monkeypatch.delenv("DETECT_DECOMPOSE", raising=False)
    else:
        monkeypatch.setenv("DETECT_DECOMPOSE", mode)

    async def fake_score(**kw: Any) -> tuple[dict[str, float], dict[str, str], set[str]]:
        if not with_scores:
            return {}, {}, set()
        return {"a-new": 0.9}, {"a-new": "incident"}, {"a-new"}

    async def fake_pairs(**kw: Any) -> tuple[dict[Any, Any], list[str]]:
        return {}, []

    monkeypatch.setattr(stateful, "_score_detect_ml", fake_score)
    monkeypatch.setattr(detect_decompose, "score_merge_pairs", fake_pairs)
    await stateful.build_estimate_stateful(
        llm=llm,  # type: ignore[arg-type]
        period_type="daily",
        repo=repo,
        store=store,
        now=datetime.now(UTC),
        db_path=db,
    )
    return store, db


def _bundles(db: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT variant, origin, outcome, final_article_ids, llm_article_ids"
            " FROM detect_bundles ORDER BY id"
        ).fetchall()


class TestWiring:
    @pytest.mark.asyncio
    async def test_default_off_records_the_current_bundle_and_opens_as_before(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        llm = _llm()

        store, db = await _run(monkeypatch, tmp_path, None, llm)

        assert [r.title for r in store.load_situations()] == [
            "新種ワイパーによる欧州電力への破壊活動"
        ]
        assert _bundles(db) == [("current", "llm", "opened", '["a-new"]', '["a-new"]')]
        assert ClusterClaimWire not in llm.schemas_seen

    @pytest.mark.asyncio
    async def test_shadow_keeps_production_result_and_only_records_the_decomposed_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        llm = _llm(_DecomposeLLM)

        store, db = await _run(monkeypatch, tmp_path, "shadow", llm)

        assert [r.title for r in store.load_situations()] == [
            "新種ワイパーによる欧州電力への破壊活動"
        ]
        rows = _bundles(db)
        assert [(r[0], r[2]) for r in rows] == [
            ("current", "opened"),
            ("decompose_shadow", "shadow_open"),
        ]
        assert rows[1][3] == '["a-new"]'

    @pytest.mark.asyncio
    async def test_on_opens_the_decomposed_claim_and_skips_the_llm_detect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        llm = _llm(_DecomposeLLM)

        store, db = await _run(monkeypatch, tmp_path, "1", llm)

        assert [r.title for r in store.load_situations()] == [
            "新種ワイパーが欧州の電力会社を破壊した"
        ]
        assert _WireDetectResult not in llm.schemas_seen
        assert [(r[0], r[1], r[2]) for r in _bundles(db)] == [("decompose", "decompose", "opened")]

    @pytest.mark.asyncio
    async def test_on_falls_back_to_the_llm_detect_when_the_decompose_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        async def boom(**kw: Any) -> None:
            raise RuntimeError("x")

        monkeypatch.setattr(detect_step, "run_decompose", boom)
        llm = _llm(_DecomposeLLM)

        store, db = await _run(monkeypatch, tmp_path, "1", llm)

        assert [r.title for r in store.load_situations()] == [
            "新種ワイパーによる欧州電力への破壊活動"
        ]
        assert [r[0] for r in _bundles(db)] == ["current"]

    @pytest.mark.asyncio
    async def test_on_without_ml_scores_falls_back_to_the_llm_detect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        llm = _llm(_DecomposeLLM)

        store, db = await _run(monkeypatch, tmp_path, "1", llm, with_scores=False)

        assert [r.title for r in store.load_situations()] == [
            "新種ワイパーによる欧州電力への破壊活動"
        ]
        assert [r[0] for r in _bundles(db)] == ["current"]

    @pytest.mark.asyncio
    async def test_a_failing_record_does_not_break_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def boom(self: Any, records: Any) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(SituationStore, "record_detect_bundles", boom)

        store, _ = await _run(monkeypatch, tmp_path, None, _llm())

        assert [r.title for r in store.load_situations()] == [
            "新種ワイパーによる欧州電力への破壊活動"
        ]

    @pytest.mark.asyncio
    async def test_a_failing_claim_id_does_not_break_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def boom(*a: Any, **k: Any) -> str:
            raise RuntimeError("x")

        monkeypatch.setattr(stateful, "make_claim_id", boom)

        store, _ = await _run(monkeypatch, tmp_path, None, _llm())

        assert len(store.load_situations()) == 1

    @pytest.mark.asyncio
    async def test_shadow_timeout_is_logged_and_does_not_affect_the_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        async def slow(*a: Any, **k: Any) -> None:
            await asyncio.sleep(5)

        monkeypatch.setattr(detect_step, "run_decompose", slow)
        monkeypatch.setattr(detect_step, "SHADOW_BUDGET_SECONDS", 0.05)

        store, db = await _run(monkeypatch, tmp_path, "shadow", _llm(_DecomposeLLM))

        assert len(store.load_situations()) == 1
        assert [r[0] for r in _bundles(db)] == ["current"]


class TestShadowCancellation:
    @pytest.mark.asyncio
    async def test_cancellation_is_propagated_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def cancelled(*a: Any, **k: Any) -> None:
            raise asyncio.CancelledError

        monkeypatch.setenv("DETECT_DECOMPOSE", "shadow")
        monkeypatch.setattr(detect_step, "run_decompose", cancelled)

        class Ctx:
            def decompose_inputs(self) -> None:
                return None

        with pytest.raises(asyncio.CancelledError):
            await detect_step.shadow_records(Ctx())  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_off_mode_runs_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DETECT_DECOMPOSE", raising=False)

        assert await detect_step.shadow_records(None) == []  # type: ignore[arg-type]


class TestMergeJoinsTheSameRunEvaluation:
    @pytest.mark.asyncio
    async def test_merged_article_is_assessed_and_recorded_on_the_existing_situation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from tests.unit.test_situation_ledger import _rev

        db, repo, store = _seed(tmp_path)
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="t2", dry_run=True)
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a-follow",
                title="続報: 別の見出し",
                url="https://x/a-follow",
                importance="high",
                status="posted",
                summary="要約。",
            )
        )
        sit = store.open_situation(
            title="既存の情勢",
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=datetime.now(UTC).isoformat(),
        )
        store.add_revision(_rev(sit.situation_id, delta="opened", created_at=_RUN_AT))
        result = DecomposeResult(
            outcomes=(ClusterOutcome(("a-follow",), "merge", situation_id=sit.situation_id),),
            overflow=0,
        )

        async def fake_score(**kw: Any) -> tuple[dict[str, float], dict[str, str], set[str]]:
            return {"a-follow": 0.9}, {"a-follow": "incident"}, {"a-follow"}

        async def fake_run(*a: Any, **k: Any) -> DecomposeResult:
            return result

        monkeypatch.setenv("DETECT_DECOMPOSE", "1")
        monkeypatch.setattr(stateful, "_score_detect_ml", fake_score)
        monkeypatch.setattr(detect_step, "run_decompose", fake_run)
        from src.synthesis.grounded.incremental import _WireIncEvidence, _WireIncHypothesis

        llm = FakeLLM(
            incremental=_WireIncremental(
                evidence=[
                    _WireIncEvidence(article_id="a-follow", excerpt="続報", polarity="supports")
                ],
                hypotheses=[_WireIncHypothesis(hypothesis="organized_state_op", consistent=2)],
                leading_hypothesis="organized_state_op",
                confidence="low",
                claim="既存の情勢に続報",
                claim_type="ongoing_activity",
            )
        )

        await stateful.build_estimate_stateful(
            llm=llm,  # type: ignore[arg-type]
            period_type="daily",
            repo=repo,
            store=store,
            now=datetime.now(UTC),
            db_path=db,
        )

        latest = store.latest_revision(sit.situation_id)
        assert latest is not None
        assert latest.rev == 2  # 同じ run で増分評価された
        assert "a-follow" in store.assigned_article_ids()
        assert _WireDetectResult not in llm.schemas_seen  # 従来の detect は呼ばれない


class TestPurgeWiring:
    def test_bundle_purge_failure_does_not_lose_the_detection_log_purge(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from src.ui.services import maintenance

        db = tmp_path / "pg.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        store.log_detection(run_at="2020-01-01T00:00:00+00:00", article_id="a", decision="rejected")

        def boom(self: Any, **k: Any) -> int:
            raise RuntimeError("x")

        monkeypatch.setattr(SituationStore, "purge_detect_bundles", boom)

        assert maintenance._purge_detection_log(repo) == 1

    def test_old_bundles_are_purged(self, tmp_path: Path) -> None:
        from src.ui.services import maintenance

        db = tmp_path / "pb.db"
        repo = RunHistoryRepository(db_path=db)
        store = SituationStore(db_path=db)
        store.record_detect_bundles(
            [BundleRecord("2020-01-01T00:00:00+00:00", "current", "c", "llm", "x", "d", (), ("a",))]
        )

        maintenance._purge_detection_log(repo)

        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM detect_bundles").fetchone() == (0,)
