"""汚染の鍵 R18: 事象の根 ∪ CVE・被害組織の共有 ∪ 埋込 cos 閾値以上 の推移閉包。

純粋関数のみ (DB は呼び出し側が行を渡す)。成分を割らずに tune / test へ寄せる。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

R18_VERSION = "R18.1"
COS_THRESHOLD = 0.80
_BLOCK = 512


def resolve_roots(merged_into: Mapping[str, str | None]) -> dict[str, str]:
    """merged_into を辿った最新の根。循環は辿り終えた時点の id で止める。"""
    roots: dict[str, str] = {}
    for start in merged_into:
        seen = {start}
        cur = start
        while True:
            nxt = merged_into.get(cur)
            if not nxt or nxt in seen:
                break
            seen.add(nxt)
            cur = nxt
        roots[start] = cur
    return roots


class _UnionFind:
    def __init__(self, items: Sequence[str]) -> None:
        self._parent = {i: i for i in items}

    def find(self, x: str) -> str:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)


def _union_by_key(uf: _UnionFind, keys_of: Mapping[str, frozenset[str]], prefix: str) -> None:
    owner: dict[str, str] = {}
    for article_id, keys in keys_of.items():
        for key in keys:
            k = f"{prefix}:{key}"
            if k in owner:
                uf.union(owner[k], article_id)
            else:
                owner[k] = article_id


def _union_by_cosine(
    uf: _UnionFind, ids: Sequence[str], vectors: np.ndarray, threshold: float
) -> None:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    unit = vectors / np.where(norms == 0, 1.0, norms)
    for start in range(0, len(ids), _BLOCK):
        sims = unit[start : start + _BLOCK] @ unit.T
        rows, cols = np.nonzero(sims >= threshold)
        for r, c in zip(rows.tolist(), cols.tolist(), strict=True):
            if start + r < c:
                uf.union(ids[start + r], ids[c])


def build_components(
    article_ids: Sequence[str],
    *,
    root_of: Mapping[str, str],
    cves: Mapping[str, frozenset[str]],
    victim_orgs: Mapping[str, frozenset[str]],
    embeddings: Mapping[str, Sequence[float]],
    cos_threshold: float = COS_THRESHOLD,
) -> dict[str, str]:
    """article_id -> 成分 id (成分内の最小 article_id)。"""
    ids = sorted(set(article_ids))
    uf = _UnionFind(ids)
    id_set = set(ids)
    _union_by_key(uf, {a: frozenset([root_of[a]]) for a in ids if a in root_of}, "root")
    _union_by_key(uf, {a: v for a, v in cves.items() if a in id_set}, "cve")
    _union_by_key(uf, {a: v for a, v in victim_orgs.items() if a in id_set}, "org")
    with_vec = [a for a in ids if a in embeddings]
    if len(with_vec) > 1:
        mat = np.asarray([embeddings[a] for a in with_vec], dtype=np.float32)
        _union_by_cosine(uf, with_vec, mat, cos_threshold)
    return {a: uf.find(a) for a in ids}


@dataclass(frozen=True)
class SplitResult:
    tune: frozenset[str]
    test: frozenset[str]
    excluded: frozenset[str]  # tune と同じ成分に入った test 側の記事 (汚染として外す)
    n_excluded_components: int


def split_by_event_time(
    event_ts: Mapping[str, datetime],
    component_of: Mapping[str, str],
    cutoff: datetime,
) -> SplitResult:
    """事象時刻 < cutoff を tune、>= cutoff を test。tune と同じ成分の test 記事は外す。"""
    tune = {a for a, t in event_ts.items() if t < cutoff}
    test = {a for a, t in event_ts.items() if t >= cutoff}
    tune_components = {component_of.get(a, a) for a in tune}
    excluded = {a for a in test if component_of.get(a, a) in tune_components}
    return SplitResult(
        frozenset(tune),
        frozenset(test - excluded),
        frozenset(excluded),
        len({component_of.get(a, a) for a in excluded}),
    )


def assert_no_shared_component(
    left: frozenset[str], right: frozenset[str], component_of: Mapping[str, str]
) -> None:
    """tune / test が同じ成分を共有していないことの検査。"""
    shared = {component_of.get(a, a) for a in left} & {component_of.get(a, a) for a in right}
    if shared:
        raise AssertionError(f"{len(shared)} components straddle the split")
