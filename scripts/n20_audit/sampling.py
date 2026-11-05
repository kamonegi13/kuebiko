"""seed 固定の層化抽出 (比例配分・最大剰余法、各層の下限 1)。"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence


def allocate(sizes: Mapping[str, int], n: int) -> dict[str, int]:
    """層の大きさ → 層ごとの標本数。比例配分 (最大剰余法)、各層は 1 以上 (層の数が n 以下のとき)。

    層の大きさを超えては割り当てない。合計が n に届かない (母集団 < n) ときは母集団全部。
    """
    strata = sorted(k for k, v in sizes.items() if v > 0)
    total = sum(sizes[k] for k in strata)
    if total <= n:
        return {k: sizes[k] for k in strata}
    alloc = dict.fromkeys(strata, 0)
    if len(strata) <= n:
        for k in strata:
            alloc[k] = 1
    remaining = n - sum(alloc.values())
    quotas = {k: remaining * sizes[k] / total for k in strata}
    for k in strata:
        alloc[k] += min(int(quotas[k]), sizes[k] - alloc[k])
    left = n - sum(alloc.values())
    order = sorted(strata, key=lambda k: (-(quotas[k] - int(quotas[k])), k))
    while left > 0:
        progressed = False
        for k in order:
            if left == 0:
                break
            if alloc[k] < sizes[k]:
                alloc[k] += 1
                left -= 1
                progressed = True
        if not progressed:
            break
    return alloc


def stratified_sample[T](items: Sequence[T], key: Callable[[T], str], n: int, seed: int) -> list[T]:
    """層ごとに seed 固定で無作為に取る。並びは層の名前順 → 層内の抽出順 (再現可能)。"""
    by: dict[str, list[T]] = defaultdict(list)
    for it in items:
        by[key(it)].append(it)
    alloc = allocate({k: len(v) for k, v in by.items()}, n)
    rng = random.Random(seed)
    out: list[T] = []
    for k in sorted(alloc):
        out.extend(rng.sample(by[k], alloc[k]))
    return out
