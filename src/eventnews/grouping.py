"""事象単位ニュースの群化規則 (docs/event_news_design.md §5)。

決定論の参加判定のみを扱う純ロジック層 (DB / LLM 非依存)。numpy は内部の
コサイン類似度計算にのみ使い、公開 API (models.py の型) は numpy 非依存を保つ。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta

import numpy as np

from src.assessment.evidence_verify import normalize_for_match
from src.eventnews.models import (
    COS_THRESHOLD,
    DORMANT_AFTER_DAYS,
    DORMANT_REJOIN_COS,
    DORMANT_REJOIN_SHARED,
    ENTITY_FREQ_CAP,
    FREQ_CAP_EXEMPT_TYPES,
    JOIN_ENTITY_TYPES,
    MEMBER_CAP,
    WINDOW_HOURS,
    Assignment,
    ItemState,
    MemberArticle,
)

# 1 記事の中に共通 entity が無いエッジは検討しない (§5 の 2 信号要求の下限)。
_MIN_SHARED_FOR_EDGE = 1

_EdgeOutcome = tuple[float, tuple[tuple[str, str], ...]] | str | None


def join_entity_key(entity_type: str, value: str) -> tuple[str, str]:
    """結合信号 entity のキー。**分母 (頻出ガードの counts) と参照側で必ず共有する**。

    victim_org だけ自由記述なので ``normalize_for_match`` で表記揺れを畳む。
    2026-08-25 まで counts 側は SQL の ``LOWER(TRIM(value))`` でキーを作っていたため
    victim_org のキーが**永久に一致せず、cap が一度も効いていなかった**
    (実測: shell / nasa 等 3 値が cap 超のまま結合信号として通っていた)。
    キーの作り方が 2 箇所にあると必ずずれるので、ここを唯一の口にする。
    """
    if entity_type == "victim_org":
        return (entity_type, normalize_for_match(value))
    return (entity_type, value)


def build_join_entities(
    raw: Iterable[tuple[str, str, str]],
    counts: Mapping[tuple[str, str], int],
) -> dict[str, frozenset[tuple[str, str]]]:
    """(article_id, entity_type, value) から結合信号となる entity 集合を組み立てる。

    - ``JOIN_ENTITY_TYPES`` (cve/victim_org/actor/malware_family) のみ採用
      (``actor_provisional`` は集合に含まれないため自動的に除外される)
    - victim_org は free-form のため ``normalize_for_match`` で正規化
    - ``counts`` (正規化後の (entity_type, value) をキーとする窓内出現記事数) が
      ``ENTITY_FREQ_CAP`` を超える値は結合信号から除外する (頻出ガード)
    """
    grouped: dict[str, set[tuple[str, str]]] = {}
    for article_id, entity_type, value in raw:
        if entity_type not in JOIN_ENTITY_TYPES:
            continue
        key = join_entity_key(entity_type, value)
        if entity_type not in FREQ_CAP_EXEMPT_TYPES and counts.get(key, 0) > ENTITY_FREQ_CAP:
            continue
        grouped.setdefault(article_id, set()).add(key)

    return {article_id: frozenset(values) for article_id, values in grouped.items()}


def _unit_vector(vec: np.ndarray) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        return arr
    return arr / norm


def _is_dormant(item: ItemState, now: datetime) -> bool:
    return (now - item.last_reported_at) >= timedelta(days=DORMANT_AFTER_DAYS)


def _within_window(anchor_ts: datetime, last_reported_at: datetime) -> bool:
    return abs((anchor_ts - last_reported_at).total_seconds()) <= WINDOW_HOURS * 3600


def _member_edges(
    cand_entities: frozenset[tuple[str, str]],
    cand_unit: np.ndarray,
    members: Sequence[MemberArticle],
    member_vecs: Mapping[str, np.ndarray],
) -> list[tuple[float, tuple[tuple[str, str], ...]]]:
    """各メンバーとの (cos, 共有 entity) を、共有 entity が存在するものだけ集める。"""
    edges: list[tuple[float, tuple[tuple[str, str], ...]]] = []
    for member in members:
        shared = tuple(sorted(cand_entities & member.entities))
        if len(shared) < _MIN_SHARED_FOR_EDGE:
            continue
        vec = member_vecs.get(member.article_id)
        if vec is None:
            continue
        cos = float(np.dot(cand_unit, _unit_vector(vec)))
        edges.append((cos, shared))
    return edges


def _item_candidate(
    item: ItemState,
    candidate: MemberArticle,
    cand_unit: np.ndarray,
    members: Sequence[MemberArticle],
    member_vecs: Mapping[str, np.ndarray],
    now: datetime,
) -> _EdgeOutcome:
    """1 アイテムに対する参加可否を判定する。

    戻り値: 参加不可 (エッジ自体が無い) は None、参加不可だが理由がある場合は
    その理由文字列 (例: 'dormant_strict')、参加可能なら (cos, 共有 entity)。
    """
    edges = _member_edges(candidate.entities, cand_unit, members, member_vecs)
    if not edges:
        return None

    if _is_dormant(item, now):
        strict = [
            (cos, shared)
            for cos, shared in edges
            if cos >= DORMANT_REJOIN_COS and len(shared) >= DORMANT_REJOIN_SHARED
        ]
        if strict:
            return max(strict, key=lambda pair: pair[0])
        normal = [(cos, shared) for cos, shared in edges if cos >= COS_THRESHOLD]
        return "dormant_strict" if normal else None

    if not _within_window(candidate.anchor_ts, item.last_reported_at):
        return None

    normal = [(cos, shared) for cos, shared in edges if cos >= COS_THRESHOLD]
    if not normal:
        return None
    return max(normal, key=lambda pair: pair[0])


def _invariant_holds(candidate: MemberArticle, members: Sequence[MemberArticle]) -> bool:
    """参加後、全メンバー + candidate が共有する entity が 1 つ以上残るか。"""
    common = candidate.entities
    for member in members:
        common = common & member.entities
        if not common:
            return False
    return True


def assign_article(
    candidate: MemberArticle,
    cand_vec: np.ndarray,
    items: Sequence[ItemState],
    item_members: Mapping[str, tuple[MemberArticle, ...]],
    member_vecs: Mapping[str, np.ndarray],
    now: datetime,
) -> Assignment:
    """1 記事をどのアイテムに参加させるか判定する (§5)。

    2 信号 (cos ≥ COS_THRESHOLD かつ entity 共有 ≥ 1) を満たすエッジを持つ
    アイテムのうち、MEMBER_CAP とアイテム不変条件を満たすものの中から
    最高 cos (同点は first_reported_at の古い方) を選ぶ。どこにも入らなければ
    ``target_item_id=None`` (呼出側が新アイテムを起こす)。
    """
    cand_unit = _unit_vector(cand_vec)
    rejected: list[str] = []
    best_item: ItemState | None = None
    best_cos = 0.0
    best_shared: tuple[tuple[str, str], ...] = ()

    for item in items:
        members = item_members.get(item.item_id, ())
        if not members:
            continue

        outcome = _item_candidate(item, candidate, cand_unit, members, member_vecs, now)
        if outcome is None:
            continue
        if isinstance(outcome, str):
            rejected.append(outcome)
            continue

        if len(members) >= MEMBER_CAP:
            rejected.append("member_cap")
            continue
        if not _invariant_holds(candidate, members):
            rejected.append("invariant")
            continue

        cos, shared = outcome
        is_better = best_item is None or cos > best_cos
        is_tie_older = (
            not is_better
            and best_item is not None
            and cos == best_cos
            and item.first_reported_at < best_item.first_reported_at
        )
        if is_better or is_tie_older:
            best_item, best_cos, best_shared = item, cos, shared

    return Assignment(
        article_id=candidate.article_id,
        target_item_id=best_item.item_id if best_item is not None else None,
        max_cos=best_cos,
        shared_entities=best_shared,
        rejected=tuple(rejected),
    )
