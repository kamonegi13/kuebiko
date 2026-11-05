"""E6 (整合の点検の規則ごとの誤りの割合) の純関数: 規則の判定・除外・標本の抜き方。

prereg docs/replay_prereg/graph_consistency_e6.md §2・§4。DB には触らない。
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from scripts.graphrag_eval.sampling import pair_key

SEED = 20261105
WINDOW_DAYS = 28
#: (層, n_k)。合計 55
LAYERS: tuple[tuple[str, int], ...] = (
    ("C1", 8),
    ("C2", 8),
    ("C3", 8),
    ("C4", 8),
    ("C5", 8),
    ("C6", 5),
    ("C10", 5),
    ("control", 5),
)
#: C2・対照で「種別の食い違い」に数えない種別
SKIPPED_KINDS = frozenset({"other", "stats", "roundup"})
#: C4: 信頼できる主題を持つべきでない記事の種別
NON_ACTOR_KINDS = frozenset({"advisory", "stats", "roundup"})
#: C5: 焦点の外れの割合の下限
OUTSIDER_RATIO = 0.2

UNIT_EVENT = "event"
UNIT_PAIR = "pair"
UNIT_ARTICLE = "article"
UNIT_TYPES: Mapping[str, str] = {
    "C1": UNIT_EVENT,
    "C2": UNIT_EVENT,
    "C3": UNIT_EVENT,
    "C4": UNIT_ARTICLE,
    "C5": UNIT_EVENT,
    "C6": UNIT_PAIR,
    "C10": UNIT_PAIR,
    "control": UNIT_PAIR,
}
DROP_REASONS = ("e2_pair", "e2_event", "guarded")


@dataclass(frozen=True)
class Unit:
    """1 つの候補 (事象・事象の組・記事)。key は層の中で一意。"""

    layer: str
    key: str
    event_ids: tuple[str, ...] = ()
    article_ids: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)


def window_start(now: datetime, days: int = WINDOW_DAYS) -> datetime:
    return now - timedelta(days=days)


def in_window(ts: datetime | None, start: datetime) -> bool:
    return ts is not None and ts >= start


def distinct_kinds(kinds: Iterable[str]) -> frozenset[str]:
    return frozenset(k for k in kinds if k and k not in SKIPPED_KINDS)


def is_c1(subjects: Iterable[str]) -> bool:
    return len(set(subjects)) >= 2


def is_c2(kinds: Iterable[str]) -> bool:
    return len(distinct_kinds(kinds)) >= 2


def is_c3(countries: Iterable[str]) -> bool:
    return len({c for c in countries if c}) >= 2


def is_c4(kind: str, trusted_subjects: Iterable[str]) -> bool:
    return kind in NON_ACTOR_KINDS and bool(set(trusted_subjects))


def is_c5(supporters: int, outsiders: int) -> bool:
    total = supporters + outsiders
    return total > 0 and outsiders / total >= OUTSIDER_RATIO


def control_pairs(events: Mapping[str, tuple[frozenset[str], frozenset[str]]]) -> list[str]:
    """同じ信頼できる主題を共有し、種別 (SKIPPED_KINDS を除く) が交わらない事象の組の鍵。

    events: 事象 id → (主題の集合, 種別の集合)。種別が片方でも空の事象は対象外。
    """
    by_subject: dict[str, list[str]] = {}
    kinds: dict[str, frozenset[str]] = {}
    for eid, (subjects, ks) in events.items():
        dk = distinct_kinds(ks)
        if not subjects or not dk:
            continue
        kinds[eid] = dk
        for s in subjects:
            by_subject.setdefault(s, []).append(eid)
    out: set[str] = set()
    for members in by_subject.values():
        ordered = sorted(members)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                if kinds[a].isdisjoint(kinds[b]):
                    out.add(pair_key(a, b))
    return sorted(out)


def exclude_units(
    units: Iterable[Unit],
    *,
    e2_pairs: frozenset[str],
    e2_events: frozenset[str],
    guarded_events: frozenset[str],
    guarded_articles: frozenset[str],
    article_events: Mapping[str, frozenset[str]] | None = None,
) -> tuple[list[Unit], dict[str, int]]:
    """除外 (prereg §2): E2 の組 → E2 の事象 → 教師の保護の記事。最初に当たった理由で数える。

    article_events: 記事 → 所属事象 (記事の層で E2 の事象に属するかを見る)。
    """
    kept: list[Unit] = []
    dropped = dict.fromkeys(DROP_REASONS, 0)
    for u in sorted(units, key=lambda x: x.key):
        events = set(u.event_ids)
        for aid in u.article_ids:
            events |= article_events.get(aid, frozenset()) if article_events else set()
        if u.key in e2_pairs:
            dropped["e2_pair"] += 1
        elif events & e2_events:
            dropped["e2_event"] += 1
        elif events & guarded_events or set(u.article_ids) & guarded_articles:
            dropped["guarded"] += 1
        else:
            kept.append(u)
    return kept, dropped


def sample_units(units: Sequence[Unit], n: int, seed: int, layer: str) -> list[Unit]:
    """適格が n 以下なら全件。超えれば鍵の昇順に並べ層ごとの種で shuffle して先頭から n 件。

    組・事象の層は、1 つの事象を層の中で 1 回まで (ハブへの偏りを防ぐ)。
    """
    pool = sorted(units, key=lambda u: u.key)
    if len(pool) <= n:
        return pool
    random.Random(f"{seed}:{layer}").shuffle(pool)
    used: set[str] = set()
    out: list[Unit] = []
    for u in pool:
        if len(out) >= n:
            break
        if used & set(u.event_ids):
            continue
        out.append(u)
        used.update(u.event_ids)
    return sorted(out, key=lambda u: u.key)


def overlap_keys(selected: Mapping[str, Sequence[Unit]]) -> dict[str, list[str]]:
    """同じ単位 (種類, 鍵) が複数の層に入ったもの → 層の一覧。"""
    seen: dict[str, list[str]] = {}
    for layer, units in selected.items():
        for u in units:
            seen.setdefault(f"{UNIT_TYPES[layer]}:{u.key}", []).append(layer)
    return {k: v for k, v in sorted(seen.items()) if len(v) > 1}


def layer_summary(
    layer: str, n_target: int, before: int, after: int, dropped: Mapping[str, int], picked: int
) -> dict[str, Any]:
    return {
        "unit": UNIT_TYPES[layer],
        "n_target": n_target,
        "N_before_exclusion": before,
        "N_after_exclusion": after,
        "excluded": dict(dropped),
        "n_selected": picked,
        "take_all": after <= n_target,
        "short_of_n": after < n_target,
        "short_uniqueness": after > n_target and picked < n_target,
    }
