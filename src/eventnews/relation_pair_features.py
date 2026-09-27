"""事象の組の特徴量 — 関係の分類器 (relation_model) の入力 (2026-09-27)。

⭐ 学習 (scripts/train_relation_model.py) と本番 (relations.relations_by_event) は
**この関数だけ** で特徴量を作る。計算を 2 か所に持つと学習と本番がずれる。

群化 ML の確率 (構成記事の組ごと) は外した: 盲検 411 組で AUC 0.915 → 0.906 と差が小さく、
候補 2 万組では重すぎる。要約埋込の重心の cos は外すと 0.906 → 0.815 に落ちるので残す。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

import numpy as np

from src.eventnews.relations import EventFeatures

if TYPE_CHECKING:
    from src.storage.run_history import RunHistoryRepository

#: 共有数と珍しさを数える指標 (relations._INDEXED と同じ)
_SHARED = ("subjects", "malware", "tools", "cves", "victims")
_SECONDS_PER_DAY = 86400.0
_CHUNK = 800

FEATURE_NAMES: tuple[str, ...] = (
    "centroid_cos",
    "gap_days",
    "kinds_disjoint",
    "kinds_known",
    *(f"mindf_{a}" for a in _SHARED),
    *(f"n_{a}" for a in _SHARED),
    "overlap",
    "roundup_both",
    "roundup_one",
    "same_country",
    "same_sector",
    "size_a",
    "size_b",
    "subject_conflict",
)


def relation_feature_vector(
    a: EventFeatures,
    b: EventFeatures,
    df: Mapping[str, Mapping[str, int]],
    centroid_cos: float,
) -> list[float]:
    """2 事象の特徴量 (``FEATURE_NAMES`` の順)。``a`` と ``b`` は報道の早い順に並べ替える。"""
    if a.first > b.first:
        a, b = b, a
    f: dict[str, float] = {"centroid_cos": centroid_cos}
    for attr in _SHARED:
        shared = getattr(a, attr) & getattr(b, attr)
        f[f"n_{attr}"] = float(len(shared))
        f[f"mindf_{attr}"] = float(min((df[attr].get(v, 0) for v in shared), default=0))
    f["gap_days"] = (b.first - a.last).total_seconds() / _SECONDS_PER_DAY
    f["overlap"] = float(b.first <= a.last)
    f["roundup_one"] = float(a.roundup != b.roundup)
    f["roundup_both"] = float(a.roundup and b.roundup)
    f["kinds_known"] = float(bool(a.kinds and b.kinds))
    f["kinds_disjoint"] = float(bool(a.kinds and b.kinds and not (a.kinds & b.kinds)))
    f["same_sector"] = float(bool(a.sectors & b.sectors))
    f["same_country"] = float(bool(a.countries & b.countries))
    f["subject_conflict"] = float(bool(a.subjects and b.subjects and not (a.subjects & b.subjects)))
    f["size_a"], f["size_b"] = float(len(a.member_ids)), float(len(b.member_ids))
    return [f[n] for n in FEATURE_NAMES]


def centroids_from_vectors(
    events: Sequence[EventFeatures], vectors: Mapping[str, np.ndarray]
) -> dict[str, np.ndarray]:
    """事象 id → 構成記事の要約埋込 (正規化) の平均を正規化したもの。

    埋込が 1 つも無い事象は含めない。
    """
    out: dict[str, np.ndarray] = {}
    for e in events:
        vs = [
            vectors[m] / (np.linalg.norm(vectors[m]) or 1.0) for m in e.member_ids if m in vectors
        ]
        if not vs:
            continue
        c = np.mean(vs, axis=0)
        out[e.item_id] = c / (np.linalg.norm(c) or 1.0)
    return out


def load_centroids(
    repo: RunHistoryRepository, events: Sequence[EventFeatures]
) -> dict[str, np.ndarray]:
    """要約埋込を DB から読み、事象ごとの重心を返す (読み込みは分割、重心だけを残す)。"""
    out: dict[str, np.ndarray] = {}
    for i in range(0, len(events), _CHUNK):
        chunk = events[i : i + _CHUNK]
        ids = [m for e in chunk for m in e.member_ids]
        out.update(centroids_from_vectors(chunk, repo.load_summary_embeddings(ids)))
    return out


def centroid_cos(centroids: Mapping[str, np.ndarray], a: str, b: str) -> float:
    """重心の cos (どちらかが無ければ 0)。"""
    ca, cb = centroids.get(a), centroids.get(b)
    return float(ca @ cb) if ca is not None and cb is not None else 0.0
