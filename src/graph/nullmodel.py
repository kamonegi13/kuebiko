"""線の型ごとの対照 (null model)。事象の属性を事象間で入れ替えても残る線は偶然と区別できない。

時期・見出し・他の属性は据え置き、線の根拠になる属性だけを並べ替えて本数を数え直す。
実測の本数を並べ替え後の平均本数で割った比 (lift) が 1 に近い型は、偶然の重なりが主体。
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from src.eventnews.relations import DerivedRelation, EventFeatures

#: 型ごとに並べ替える属性 (まとめて 1 組として入れ替える)
SHUFFLE_FIELDS: Mapping[str, tuple[str, ...]] = {
    "same_nation": ("subjects",),
    "same_capability": ("malware", "tools", "malware_head", "tools_head"),
    "same_target": ("countries", "countries_head"),
}

Deriver = Callable[[list[EventFeatures]], list[DerivedRelation]]


def shuffled_events(
    events: Sequence[EventFeatures], fields: tuple[str, ...], rng: random.Random
) -> list[EventFeatures]:
    """``fields`` の値の組を事象間で入れ替えた事象列 (元は変えない)。"""
    order = list(range(len(events)))
    rng.shuffle(order)
    return [
        replace(e, **{f: getattr(events[src], f) for f in fields})
        for e, src in zip(events, order, strict=True)
    ]


def count_by_type(rels: Sequence[DerivedRelation]) -> Counter[str]:
    return Counter(r.rel_type for r in rels)


@dataclass(frozen=True)
class NullResult:
    """型ごとの対照の結果。``samples`` は並べ替えごとの本数。"""

    real: int
    samples: tuple[int, ...]

    @property
    def mean(self) -> float:
        return sum(self.samples) / len(self.samples)

    @property
    def lift(self) -> float | None:
        return (self.real / self.mean) if self.mean > 0 else None

    def null_interval(self, level: float = 0.95) -> tuple[float, float]:
        """並べ替え後の本数の中央 ``level`` 区間 (パーセンタイル)。"""
        xs = sorted(self.samples)
        lo = (1 - level) / 2
        return _percentile(xs, lo), _percentile(xs, 1 - lo)

    def lift_interval(self, level: float = 0.95) -> tuple[float | None, float | None]:
        """lift の区間 (実測 / 並べ替え後の本数の上下端)。端が 0 の側は None。"""
        lo, hi = self.null_interval(level)
        return (self.real / hi if hi > 0 else None, self.real / lo if lo > 0 else None)

    @property
    def is_degenerate(self) -> bool:
        """並べ替えで本数が一切動かない (対照が型の判別に使えない: 時期等の条件を持たない型)。"""
        return len(set(self.samples)) == 1

    def is_chance_level(self, level: float = 0.95) -> bool:
        """実測が並べ替え後の区間に収まる (偶然と区別できない)。"""
        lo, hi = self.null_interval(level)
        return lo <= self.real <= hi


def _percentile(sorted_xs: Sequence[int], q: float) -> float:
    pos = q * (len(sorted_xs) - 1)
    base = int(pos)
    nxt = min(base + 1, len(sorted_xs) - 1)
    return sorted_xs[base] + (sorted_xs[nxt] - sorted_xs[base]) * (pos - base)


def null_results(
    events: Sequence[EventFeatures],
    derive: Deriver,
    *,
    rounds: int = 20,
    seed: int = 0,
    fields: Mapping[str, tuple[str, ...]] = SHUFFLE_FIELDS,
) -> dict[str, NullResult]:
    """型ごとの対照 (実測本数と、並べ替えごとの本数)。"""
    if rounds < 1:
        raise ValueError("rounds must be >= 1")
    real = count_by_type(derive(list(events)))
    rng = random.Random(seed)
    out: dict[str, NullResult] = {}
    for rel_type, cols in fields.items():
        samples = tuple(
            count_by_type(derive(shuffled_events(events, cols, rng))).get(rel_type, 0)
            for _ in range(rounds)
        )
        out[rel_type] = NullResult(real.get(rel_type, 0), samples)
    return out


def null_lift(
    events: Sequence[EventFeatures],
    derive: Deriver,
    *,
    rounds: int = 20,
    seed: int = 0,
    fields: Mapping[str, tuple[str, ...]] = SHUFFLE_FIELDS,
) -> dict[str, tuple[int, float, float | None]]:
    """型ごとに (実測本数, 並べ替え後の平均本数, lift)。平均 0 の型の lift は None。"""
    res = null_results(events, derive, rounds=rounds, seed=seed, fields=fields)
    return {t: (r.real, r.mean, r.lift) for t, r in res.items()}
