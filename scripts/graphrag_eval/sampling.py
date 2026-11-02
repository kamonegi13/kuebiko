"""E2 の標本の抜き方と除外 (純関数、prereg graphrag_e2_edge_precision.md §2・§4)。"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass


def pair_key(a: str, b: str) -> str:
    """向きに依らない組の鍵 (事象 id を昇順に並べる)。"""
    x, y = sorted((a, b))
    return f"{x}|{y}"


def resolve_root(item_id: str, merged_into: Mapping[str, str | None]) -> str:
    """merged_into を辿った根 (循環は打ち切る)。"""
    seen = {item_id}
    cur = item_id
    while True:
        nxt = merged_into.get(cur) or ""
        if not nxt or nxt in seen:
            return cur
        seen.add(nxt)
        cur = nxt


@dataclass(frozen=True)
class ExclusionResult:
    kept: list[str]  # 組の鍵
    dropped: dict[str, int]  # 理由 → 組の数 (最初に当たった理由で数える)


def exclude_pairs(
    keys: Iterable[str],
    *,
    excluded_pairs: frozenset[str],
    excluded_events: frozenset[str],
    excluded_roots: frozenset[str],
    root_of: Mapping[str, str],
    guarded_events: frozenset[str],
) -> ExclusionResult:
    """除外の順: 過去の標本の組 → 過去の標本の事象 → 同じ根 (R18 の事象の根) → 保護した記事を含む事象。"""
    kept: list[str] = []
    dropped = {"prior_pair": 0, "prior_event": 0, "prior_root": 0, "guarded_article": 0}
    for k in sorted(set(keys)):
        a, b = k.split("|")
        if k in excluded_pairs:
            dropped["prior_pair"] += 1
        elif a in excluded_events or b in excluded_events:
            dropped["prior_event"] += 1
        elif root_of.get(a, a) in excluded_roots or root_of.get(b, b) in excluded_roots:
            dropped["prior_root"] += 1
        elif a in guarded_events or b in guarded_events:
            dropped["guarded_article"] += 1
        else:
            kept.append(k)
    return ExclusionResult(kept, dropped)


def sample_pairs(keys: Sequence[str], n: int, seed: int) -> list[str]:
    """鍵を昇順に並べて shuffle し、先頭から取る。1 つの事象は型の中で 1 回まで (ハブの偏りを防ぐ)。"""
    pool = sorted(set(keys))
    random.Random(seed).shuffle(pool)
    used: set[str] = set()
    out: list[str] = []
    for k in pool:
        if len(out) >= n:
            break
        a, b = k.split("|")
        if a in used or b in used:
            continue
        out.append(k)
        used.update((a, b))
    return out


def pick_members(member_ids_by_time: Sequence[str], max_n: int) -> list[str]:
    """事象の構成記事 (時刻順) から提示する記事: 多ければ最初の半分と最後の半分。"""
    if len(member_ids_by_time) <= max_n:
        return list(member_ids_by_time)
    head = max_n - max_n // 2
    return [*member_ids_by_time[:head], *member_ids_by_time[-(max_n // 2) :]]


def choose_sample(
    kept: Sequence[str], n: int, seed: int, *, must_include: frozenset[str] = frozenset()
) -> list[str]:
    """適格が n 以下なら全件 (事象の制約なし)。超えれば must_include を全て取り、残りを sample_pairs で埋める。"""
    pool = sorted(set(kept))
    if len(pool) <= n:
        return pool
    fixed = [k for k in pool if k in must_include]
    rest = [k for k in pool if k not in must_include]
    return fixed + sample_pairs(rest, max(0, n - len(fixed)), seed)


def supplement_pool(relaxed_kept: Sequence[str], original: Iterable[str]) -> list[str]:
    """除外を緩めた適格のうち、元の標本に無い組 (昇順)。"""
    seen = set(original)
    return sorted(k for k in set(relaxed_kept) if k not in seen)
