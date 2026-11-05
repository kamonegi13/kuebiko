"""RP-2 (pair の LLM なしの判定) の対の単位の純粋部分。

prereg: docs/replay_prereg/pair_llm_off.md §5.1-5.2, §6。

DB・LLM・Opus は使わない。入力は記録の特徴 (23 欄) と記録の llm_same。両腕は本番の
``PairModel`` の関数 (probability / joins) をそのまま通す (記録の ml_joined は判定に使わない)。
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from scripts.replay._stats import mcnemar_exact
from src.eventnews.pair_model import PairModel

BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 20261102
CONFIDENCE_ALPHA = 0.025
SAME_LABEL = "same"
HARD_STRATA = (
    "format_diff",
    "cve_other_action",
    "actor_other_op",
    "roundup_vs_item",
    "bulletin_vs_item",
    "same_format_other",
)
# 合否の線 (prereg §6、結果を見る前に固定)
MAX_RECALL_DROP_POINT = 0.02
MAX_RECALL_DROP_UPPER = 0.06
MIN_PRECISION_DELTA_LOWER = -0.03


@dataclass(frozen=True)
class Pair:
    f3_id: str
    stratum: str
    weight: float
    label: str
    features: tuple[float, ...]  # 記録の特徴 (llm の 2 欄を除く)
    llm_same: int  # 記録の s23i の判定 (0/1)

    @property
    def is_same(self) -> bool:
        return self.label == SAME_LABEL


def decide(
    model: PairModel, features: Sequence[float], *, llm_same: int | None
) -> tuple[float, bool]:
    """(確率, 結合) を本番と同じ組み立てで出す。llm_same=None は LLM なし。

    LLM なしは ``llm_same``/``llm_known`` = (0, 0) + ``threshold_llm_off``。
    """
    known = llm_same is not None
    full = [*features, 1.0 if llm_same else 0.0, 1.0 if known else 0.0]
    return model.probability(full), model.joins(full, llm_available=known)


def arm_joins(
    model: PairModel,
    pairs: Sequence[Pair],
    *,
    llm_off: bool,
    llm_same: Mapping[str, int] | None = None,
) -> list[bool]:
    """腕ごとの結合。llm_off=True が出荷する構成、False は記録 (または渡した) llm_same。"""
    out: list[bool] = []
    for p in pairs:
        if llm_off:
            same: int | None = None
        else:
            same = llm_same[p.f3_id] if llm_same is not None else p.llm_same
        out.append(decide(model, p.features, llm_same=same)[1])
    return out


def recall_precision(
    pairs: Sequence[Pair], joined: Sequence[bool], *, weighted: bool
) -> tuple[float | None, float | None]:
    """(回収 R, 適合 P)。分母 0 は None。"""
    pos = hit = join = 0.0
    for p, j in zip(pairs, joined, strict=True):
        w = p.weight if weighted else 1.0
        pos += w if p.is_same else 0.0
        join += w if j else 0.0
        hit += w if (j and p.is_same) else 0.0
    return (hit / pos if pos else None, hit / join if join else None)


def _percentile(sorted_draws: Sequence[float], q: float) -> float:
    return sorted_draws[min(len(sorted_draws) - 1, max(0, round(q * (len(sorted_draws) - 1))))]


@dataclass(frozen=True)
class Interval:
    point: float
    low: float
    high: float
    n_draws: int


def _rp(pairs: Sequence[Pair], joined: Sequence[bool]) -> tuple[float, float] | None:
    r, p = recall_precision(pairs, joined, weighted=True)
    return None if r is None or p is None else (r, p)


def paired_bootstrap(
    pairs: Sequence[Pair],
    base: Sequence[bool],
    other: Sequence[bool],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[Interval, Interval]:
    """(ΔR = R(base) - R(other), ΔP = P(other) - P(base))。

    層の中で組を復元抽出し、同じ組に両腕を当てる。point は復元抽出をしない元の標本の値、
    区間は 95% パーセンタイル。
    """
    full_b, full_o = _rp(pairs, base), _rp(pairs, other)
    if full_b is None or full_o is None:
        raise ValueError("recall/precision undefined on the full sample")
    by_stratum: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(pairs):
        by_stratum[p.stratum].append(i)
    rng = random.Random(seed)
    d_r: list[float] = []
    d_p: list[float] = []
    for _ in range(iterations):
        idx = [rng.choice(members) for members in by_stratum.values() for _ in members]
        sample = [pairs[i] for i in idx]
        b = _rp(sample, [base[i] for i in idx])
        o = _rp(sample, [other[i] for i in idx])
        if b is None or o is None:
            continue
        d_r.append(b[0] - o[0])
        d_p.append(o[1] - b[1])
    d_r.sort()
    d_p.sort()
    hi = 1 - CONFIDENCE_ALPHA
    return (
        Interval(
            full_b[0] - full_o[0],
            _percentile(d_r, CONFIDENCE_ALPHA),
            _percentile(d_r, hi),
            len(d_r),
        ),
        Interval(
            full_o[1] - full_b[1],
            _percentile(d_p, CONFIDENCE_ALPHA),
            _percentile(d_p, hi),
            len(d_p),
        ),
    )


def mcnemar_counts(
    pairs: Sequence[Pair], on: Sequence[bool], off: Sequence[bool]
) -> dict[str, float | int]:
    """same の組の回収の不一致と、same 以外の組の誤結合の不一致 (報告のみ、合否に使わない)。"""
    triples = list(zip(pairs, on, off, strict=True))
    rec_on = sum(1 for p, a, b in triples if p.is_same and a and not b)
    rec_off = sum(1 for p, a, b in triples if p.is_same and b and not a)
    fp_on = sum(1 for p, a, b in triples if not p.is_same and a and not b)
    fp_off = sum(1 for p, a, b in triples if not p.is_same and b and not a)
    return {
        "recall_only_on": rec_on,
        "recall_only_off": rec_off,
        "recall_p": mcnemar_exact(rec_on, rec_off),
        "false_join_only_on": fp_on,
        "false_join_only_off": fp_off,
        "false_join_p": mcnemar_exact(fp_on, fp_off),
    }


def stratum_report(
    pairs: Sequence[Pair], on: Sequence[bool], off: Sequence[bool], strata: Sequence[str]
) -> dict[str, dict[str, float | int | None]]:
    """層ごとの重みなしの報告 (判定には使わない)。"""
    out: dict[str, dict[str, float | int | None]] = {}
    for s in strata:
        idx = [i for i, p in enumerate(pairs) if p.stratum == s]
        sub = [pairs[i] for i in idx]
        r_on, p_on = recall_precision(sub, [on[i] for i in idx], weighted=False)
        r_off, p_off = recall_precision(sub, [off[i] for i in idx], weighted=False)
        out[s] = {
            "n": len(sub),
            "n_same": sum(1 for p in sub if p.is_same),
            "recall_on": r_on,
            "recall_off": r_off,
            "precision_on": p_on,
            "precision_off": p_off,
        }
    return out


def verdict_pair_level(d_recall: Interval, d_precision: Interval) -> dict[str, bool]:
    """prereg §6 の 1 (回収) と 2 (適合)。3 (群の単位) は別の実行。"""
    return {
        "criterion_1_recall": d_recall.point <= MAX_RECALL_DROP_POINT
        and d_recall.high <= MAX_RECALL_DROP_UPPER,
        "criterion_2_precision": d_precision.low >= MIN_PRECISION_DELTA_LOWER,
    }
