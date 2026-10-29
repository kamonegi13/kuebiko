"""replay の統計部品 (標準ライブラリのみ)。

Wilson 区間 / McNemar 正確検定 / 3 の規則 / 層の重み付き推定 + クラスタ bootstrap。
乱数は必ず種を受け取る (prereg に種を書くため)。
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

Z_95 = 1.959963984540054
DEFAULT_BOOTSTRAP_ITERATIONS = 2000


@dataclass(frozen=True)
class Interval:
    point: float
    low: float
    high: float


def wilson_interval(successes: int, n: int, z: float = Z_95) -> Interval:
    """二項比率の Wilson 区間。n=0 は (0,0,1) とする (情報なし)。"""
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"invalid counts: successes={successes} n={n}")
    if n == 0:
        return Interval(0.0, 0.0, 1.0)
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return Interval(p, max(0.0, center - half), min(1.0, center + half))


def rule_of_three(n: int) -> float:
    """0 件観測のときの 95% 上側限界 (約 3/n)。"""
    if n <= 0:
        raise ValueError("n must be positive")
    return min(1.0, 3.0 / n)


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """McNemar の正確検定 (両側)。引数は不一致の組の数 (A だけ正 / B だけ正)。"""
    if only_a < 0 or only_b < 0:
        raise ValueError("counts must be non-negative")
    n = only_a + only_b
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(only_a, only_b) + 1))
    return min(1.0, 2.0 * tail / (1 << n))


def discordant_counts(pairs: Sequence[tuple[bool, bool]]) -> tuple[int, int]:
    """(A 正, B 正) の対から不一致 (A だけ正, B だけ正) を数える。"""
    only_a = sum(1 for a, b in pairs if a and not b)
    only_b = sum(1 for a, b in pairs if b and not a)
    return only_a, only_b


@dataclass(frozen=True)
class Observation:
    cluster: str  # 事象の根 or 記事 id
    value: float  # 0/1 など


def weighted_estimate(
    strata: Mapping[str, Sequence[Observation]], weights: Mapping[str, float]
) -> float:
    """層の重み付き平均。重みは使う層で正規化する。空の層は重みから外す。"""
    used = {s: obs for s, obs in strata.items() if obs}
    if not used:
        raise ValueError("no observations")
    missing = [s for s in used if s not in weights]
    if missing:
        raise ValueError(f"weights missing for strata: {sorted(missing)}")
    total = sum(weights[s] for s in used)
    if total <= 0:
        raise ValueError("weights must sum to a positive value")
    acc = 0.0
    for s, obs in used.items():
        acc += weights[s] * sum(o.value for o in obs) / len(obs)
    return acc / total


def _group_by_cluster(obs: Sequence[Observation]) -> list[list[float]]:
    groups: dict[str, list[float]] = {}
    for o in obs:
        groups.setdefault(o.cluster, []).append(o.value)
    return list(groups.values())


def cluster_bootstrap_ci(
    strata: Mapping[str, Sequence[Observation]],
    weights: Mapping[str, float],
    *,
    seed: int,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence: float = 0.95,
) -> Interval:
    """層の中でクラスタ (事象の根/記事) を復元抽出し、重み付き推定の百分位区間を返す。"""
    if iterations < 1 or not 0 < confidence < 1:
        raise ValueError("invalid bootstrap settings")
    point = weighted_estimate(strata, weights)
    grouped = {s: _group_by_cluster(obs) for s, obs in strata.items() if obs}
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(iterations):
        resampled: dict[str, list[Observation]] = {}
        for s, clusters in grouped.items():
            picked = [rng.choice(clusters) for _ in clusters]
            resampled[s] = [Observation("b", v) for c in picked for v in c]
        draws.append(weighted_estimate(resampled, weights))
    draws.sort()
    alpha = (1 - confidence) / 2
    low = draws[math.floor(alpha * (iterations - 1))]
    high = draws[math.ceil((1 - alpha) * (iterations - 1))]
    return Interval(point, low, high)
