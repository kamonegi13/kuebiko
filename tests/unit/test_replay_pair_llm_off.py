"""RP-2 (pair の LLM なし) の対の単位の純粋部分と、旗 0 の経路との一致。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import numpy as np

from scripts.replay.pair_llm_off_core import (
    Interval,
    Pair,
    arm_joins,
    decide,
    mcnemar_counts,
    paired_bootstrap,
    recall_precision,
    verdict_pair_level,
)
from src.eventnews import pair_model, pair_shadow
from src.eventnews.models import MemberArticle

MODEL = pair_model.load_model("config/models/pair_model.json")


def _pair(i: int, label: str, weight: float = 1.0) -> Pair:
    feats = tuple([0.5] * 23)
    return Pair(f"p{i}", "s1" if i % 2 else "s2", weight, label, feats, 1)


def _member(aid: str) -> MemberArticle:
    return MemberArticle(
        article_id=aid, title=f"t {aid}", url=f"https://kuebiko.example/{aid}", feed_title="f",
        feed_url="https://kuebiko.example/f", host="kuebiko.example", importance="medium",
        category="malware", status="posted", anchor_ts=datetime(2026, 10, 1, tzinfo=UTC),
        summary="s", body="", entities=frozenset({("cve", "cve-2026-0001")}),
    )  # fmt: skip


def test_llm_off_arm_equals_judge_pairs_without_llm() -> None:
    assert MODEL is not None
    vec = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
    pairs = [(_member(f"a{i}"), _member(f"b{i}")) for i in range(10)]
    vectors = {m.article_id: vec for pr in pairs for m in pr}

    async def embed(arts: object) -> dict[str, np.ndarray]:
        return {a.article_id: vec for a in arts}  # type: ignore[attr-defined]

    verdicts = asyncio.run(pair_shadow.judge_pairs(pairs, vectors, llm=None, embed_summary=embed))
    assert len(verdicts) == 10
    for v in verdicts:
        proba, joined = decide(MODEL, v.features, llm_same=None)
        assert v.ml_proba == proba
        assert v.ml_joined == joined


def test_arm_joins_uses_off_threshold_when_llm_off() -> None:
    assert MODEL is not None
    p = _pair(1, "same")
    off = arm_joins(MODEL, [p], llm_off=True)[0]
    assert off == decide(MODEL, p.features, llm_same=None)[1]


def test_recall_precision_weighted() -> None:
    pairs = [_pair(1, "same", 3.0), _pair(2, "same", 1.0), _pair(3, "different", 4.0)]
    r, p = recall_precision(pairs, [True, False, True], weighted=True)
    assert r == 3.0 / 4.0
    assert p == 3.0 / 7.0


def test_bootstrap_is_deterministic_and_identical_arms_give_zero() -> None:
    pairs = [_pair(i, "same" if i % 3 else "different", 1.0 + i) for i in range(30)]
    joined = [i % 2 == 0 for i in range(30)]
    a = paired_bootstrap(pairs, joined, joined, iterations=200)
    b = paired_bootstrap(pairs, joined, joined, iterations=200)
    assert a == b
    assert a[0].point == 0.0 and a[0].low == 0.0 and a[0].high == 0.0


def test_mcnemar_counts_directions() -> None:
    pairs = [_pair(1, "same"), _pair(2, "same"), _pair(3, "different")]
    c = mcnemar_counts(pairs, [True, False, False], [False, True, True])
    assert (c["recall_only_on"], c["recall_only_off"], c["false_join_only_off"]) == (1, 1, 1)


def test_verdict_thresholds() -> None:
    ok = verdict_pair_level(Interval(0.02, -0.01, 0.06, 10), Interval(0.0, -0.03, 0.02, 10))
    assert all(ok.values())
    bad = verdict_pair_level(Interval(0.021, -0.01, 0.05, 10), Interval(0.0, -0.031, 0.02, 10))
    assert not bad["criterion_1_recall"] and not bad["criterion_2_precision"]
