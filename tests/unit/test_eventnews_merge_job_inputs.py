"""統合の段の入力を学習時の形に戻す (2026-10-22)。

pair ML の学習時は (a) 記事の種別を特徴に持ち、(b) 要約埋込が全記事にあった。
統合の段は種別を全件 "other" に固定し (記事の 91% に種別が付いた今は不要な縮退)、
要約埋込が無い記事 (半数) を本文 cos で代用していた。``EVENTNEWS_MERGE_REAL_KIND=1`` で戻す。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast

import numpy as np
import pytest

from src.eventnews.merge import MIN_EDGES_BETWEEN_ITEMS
from src.ui.services import eventnews_merge_job as job
from tests.unit.test_eventnews_merge_job import _inputs, _member


class _Model:
    """採点だけ記録する偽モデル。"""

    def joins(self, features: list[float], *, llm_available: bool) -> bool:
        return True

    def probability(self, features: list[float]) -> float:
        return 0.77


def _capture_kinds(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    def fake_features(left: Any, right: Any) -> list[float]:
        seen.append((left.kind, right.kind))
        return [0.0] * job_feature_count()

    monkeypatch.setattr(job, "pair_features", fake_features)
    return seen


def job_feature_count() -> int:
    from src.eventnews.pair_features import FEATURE_NAMES

    return len(FEATURE_NAMES)


def _inputs_with_kinds() -> job.MergeInputs:
    base = _inputs()
    members = {
        "a1": replace(base.members["a1"], kind="advisory"),
        "a2": replace(base.members["a2"], kind=""),  # 未分類
        "b1": replace(base.members["b1"], kind="exploitation"),
    }
    return replace(base, members=members)


class TestRealKind:
    def test_default_is_off_and_kinds_are_fixed_to_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EVENTNEWS_MERGE_REAL_KIND", raising=False)
        seen = _capture_kinds(monkeypatch)

        assert job.real_kind_enabled() is False
        job.approve_pairs(_inputs_with_kinds(), cast(job.PairModel, _Model()))

        assert seen and set(seen) == {("other", "other")}

    def test_on_reads_the_stored_kinds_and_falls_back_to_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVENTNEWS_MERGE_REAL_KIND", "1")
        seen = _capture_kinds(monkeypatch)

        assert job.real_kind_enabled() is True
        job.approve_pairs(_inputs_with_kinds(), cast(job.PairModel, _Model()))

        assert set(seen) == {("advisory", "exploitation"), ("other", "exploitation")}

    def test_explicit_argument_overrides_the_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVENTNEWS_MERGE_REAL_KIND", "1")
        seen = _capture_kinds(monkeypatch)

        job.approve_pairs(_inputs_with_kinds(), cast(job.PairModel, _Model()), real_kind=False)

        assert set(seen) == {("other", "other")}


class TestProbaCollection:
    def test_probabilities_are_collected_for_every_scored_pair(self) -> None:
        probas: dict[tuple[str, str], float] = {}

        job.approve_pairs(_inputs(), cast(job.PairModel, _Model()), probas_out=probas)

        assert probas == {("a1", "b1"): 0.77, ("a2", "b1"): 0.77}


class TestMaxGap:
    def test_default_is_sixty_days(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EVENTNEWS_MERGE_MAX_GAP_DAYS", raising=False)
        assert job.max_gap() == timedelta(days=60)

    def test_zero_means_unbounded_and_garbage_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVENTNEWS_MERGE_MAX_GAP_DAYS", "0")
        assert job.max_gap() is None
        monkeypatch.setenv("EVENTNEWS_MERGE_MAX_GAP_DAYS", "abc")
        assert job.max_gap() == timedelta(days=60)
        monkeypatch.setenv("EVENTNEWS_MERGE_MAX_GAP_DAYS", "14")
        assert job.max_gap() == timedelta(days=14)


class TestFlagSnapshot:
    def test_snapshot_records_every_input_that_changes_the_decision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVENTNEWS_MERGE_HUB_GATE", "1")
        monkeypatch.setenv("EVENTNEWS_MERGE_REAL_KIND", "1")

        snap = job.merge_flag_snapshot()

        assert snap["hub_gate"] is True
        assert snap["real_kind"] is True
        assert {
            "cve_gate",
            "bulletin_bridge_gate",
            "report_period_gate",
            "frequent",
            "singleton_rescue",
            "max_gap_days",
            "min_edges",
        } <= set(snap)


class _EmbedRepo:
    pass


class TestFillSummaryEmbeddings:
    def test_fills_only_missing_ones_up_to_the_cap_newest_first(self) -> None:
        base = _inputs()
        base = replace(base, summary_vectors={"a1": np.ones(2, dtype=np.float32)})
        asked: list[list[str]] = []

        async def fake_embed(
            config: Any, arts: list[Any], *, repo: Any = None
        ) -> dict[str, np.ndarray]:
            asked.append([a.article_id for a in arts])
            return {a.article_id: np.full(2, 0.5, dtype=np.float32) for a in arts}

        out, n = asyncio.run(
            job.fill_summary_embeddings(
                cast(Any, _EmbedRepo()), cast(Any, None), base, cap=1, embed=fake_embed
            )
        )

        assert n == 1
        assert len(asked) == 1 and len(asked[0]) == 1
        assert asked[0][0] in {"a2", "b1"}  # a1 は既にあるので頼まない
        assert len(out.summary_vectors) == 2
        assert len(base.summary_vectors) == 1  # 元の入力は変えない (不変)

    def test_nothing_to_do_does_not_call_the_embedder(self) -> None:
        base = _inputs()
        base = replace(
            base, summary_vectors={k: np.ones(2, dtype=np.float32) for k in base.members}
        )

        async def boom(*_a: Any, **_k: Any) -> dict[str, np.ndarray]:
            raise AssertionError("called")

        out, n = asyncio.run(
            job.fill_summary_embeddings(
                cast(Any, _EmbedRepo()), cast(Any, None), base, cap=10, embed=boom
            )
        )

        assert n == 0 and out is base

    def test_embedder_failure_is_not_fatal(self) -> None:
        base = _inputs()

        async def broken(*_a: Any, **_k: Any) -> dict[str, np.ndarray]:
            raise RuntimeError("ollama down")

        out, n = asyncio.run(
            job.fill_summary_embeddings(
                cast(Any, _EmbedRepo()), cast(Any, None), base, cap=10, embed=broken
            )
        )

        assert n == 0 and out is base

    def test_roundup_members_are_not_embedded(self) -> None:
        base = _inputs()
        members = {**base.members, "r1": replace(_member("r1"), kind="roundup")}
        base = replace(base, members=members, item_of={**base.item_of, "r1": "i2"})
        asked: list[str] = []

        async def fake_embed(
            config: Any, arts: list[Any], *, repo: Any = None
        ) -> dict[str, np.ndarray]:
            asked.extend(a.article_id for a in arts)
            return {a.article_id: np.ones(2, dtype=np.float32) for a in arts}

        asyncio.run(
            job.fill_summary_embeddings(
                cast(Any, _EmbedRepo()), cast(Any, None), base, cap=10, embed=fake_embed
            )
        )

        assert "r1" not in asked


def _plan_with_one_group() -> job.MergePlan:
    from src.eventnews.merge import MergeGroup, MergeLink

    group = MergeGroup(
        target="i1",
        absorbed=("i2",),
        links=(MergeLink("i1", "i2", (("a1", "b1"), ("a2", "b1"))),),
    )
    return job.MergePlan(groups=(group,), inputs=_inputs(), probas={("a1", "b1"): 0.9})


class TestRecordDecisions:
    def test_records_one_row_per_absorbed_item_with_flags(self, tmp_path: Any) -> None:
        import json
        from datetime import UTC, datetime

        from src.storage.run_history import RunHistoryRepository

        repo = RunHistoryRepository(db_path=tmp_path / "d.db")
        now = datetime(2026, 10, 22, tzinfo=UTC)

        n = job.record_decisions(repo, _plan_with_one_group(), now, run_kind="manual")

        assert n == 1
        (row,) = repo.list_merge_decisions()
        assert (row.target_id, row.absorbed_id, row.run_kind) == ("i1", "i2", "manual")
        assert row.edge_count == 2
        assert json.loads(row.flags_json)["min_edges"] == MIN_EDGES_BETWEEN_ITEMS
        probas = {(e["a"], e["b"]): e["proba"] for e in json.loads(row.edges_json)}
        assert probas == {("a1", "b1"): 0.9, ("a2", "b1"): None}

    def test_a_recording_failure_does_not_raise(self) -> None:
        from datetime import UTC, datetime

        class _Broken:
            def record_merge_decisions(self, *_a: object, **_k: object) -> int:
                raise RuntimeError("db down")

        now = datetime(2026, 10, 22, tzinfo=UTC)

        n = job.record_decisions(cast(Any, _Broken()), _plan_with_one_group(), now, run_kind="x")

        assert n == 0

    def test_nothing_to_record_without_groups(self) -> None:
        from datetime import UTC, datetime

        now = datetime(2026, 10, 22, tzinfo=UTC)

        assert job.record_decisions(cast(Any, None), job.MergePlan(), now, run_kind="x") == 0
