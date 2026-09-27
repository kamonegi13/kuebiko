"""事象どうしの関係の分類器 (2026-09-27、src/eventnews/relation_model.py) — 推論・読込・特徴量。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from src.eventnews.relation_model import RelationModel, load_relation_model
from src.eventnews.relation_pair_features import (
    FEATURE_NAMES,
    centroid_cos,
    centroids_from_vectors,
    relation_feature_vector,
)
from src.eventnews.relations import EventFeatures

_T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _stump(feature: int, threshold: float, left_p: float, right_p: float) -> dict[str, list[float]]:
    """根 1 つ + 葉 2 つの木 (葉は children_left == -1)。"""
    return {
        "left": [1, -1, -1],
        "right": [2, -1, -1],
        "feature": [feature, -2, -2],
        "threshold": [threshold, -2.0, -2.0],
        "proba": [0.0, left_p, right_p],
    }


def _model(*trees: dict[str, list[float]]) -> RelationModel:
    return RelationModel(feature_names=FEATURE_NAMES, threshold=0.5, _trees=tuple(trees))


def test_probabilities_follow_each_tree_and_average() -> None:
    cos = FEATURE_NAMES.index("centroid_cos")
    model = _model(_stump(cos, 0.6, 0.1, 0.9), _stump(cos, 0.8, 0.0, 1.0))
    x = np.zeros((3, len(FEATURE_NAMES)))
    x[:, cos] = [0.5, 0.7, 0.9]

    got = model.probabilities(x)

    assert got.tolist() == pytest.approx([0.05, 0.45, 0.95])


def test_probabilities_reject_wrong_width() -> None:
    with pytest.raises(ValueError, match="特徴量の形"):
        _model(_stump(0, 0.5, 0.0, 1.0)).probabilities(np.zeros((2, 3)))


def test_load_returns_none_when_missing_or_mismatched(tmp_path: Path) -> None:
    missing = tmp_path / "none.json"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"feature_names": ["x"], "threshold": 0.5, "trees": []}))
    load_relation_model.cache_clear()

    assert load_relation_model(str(missing)) is None
    assert load_relation_model(str(bad)) is None


def test_load_reads_a_valid_model(tmp_path: Path) -> None:
    path = tmp_path / "ok.json"
    tree = _stump(0, 0.5, 0.0, 1.0)
    path.write_text(
        json.dumps({"feature_names": list(FEATURE_NAMES), "threshold": 0.8, "trees": [tree]})
    )
    load_relation_model.cache_clear()

    model = load_relation_model(str(path))

    assert model is not None
    assert model.threshold == 0.8


def _e(item_id: str, day: int, **kw: object) -> EventFeatures:
    first = _T0 + timedelta(days=day)
    return EventFeatures(item_id=item_id, first=first, last=first + timedelta(days=1), **kw)  # type: ignore[arg-type]


def test_feature_vector_is_symmetric_and_named() -> None:
    a = _e("a", 0, victims=frozenset({"acme"}), member_ids=("x1",))
    b = _e("b", 10, victims=frozenset({"acme"}), member_ids=("y1", "y2"))
    df: dict[str, dict[str, int]] = {
        k: {} for k in ("subjects", "malware", "tools", "cves", "victims")
    }
    df["victims"] = {"acme": 2}

    ab = relation_feature_vector(a, b, df, 0.7)
    ba = relation_feature_vector(b, a, df, 0.7)

    assert ab == ba  # 報道の早い順に並べ替える
    f = dict(zip(FEATURE_NAMES, ab, strict=True))
    assert f["n_victims"] == 1.0
    assert f["mindf_victims"] == 2.0
    assert f["gap_days"] == pytest.approx(9.0)
    assert (f["size_a"], f["size_b"]) == (1.0, 2.0)


def test_centroids_average_normalized_member_vectors() -> None:
    events = [_e("a", 0, member_ids=("x", "y")), _e("b", 1, member_ids=("z",)), _e("c", 2)]
    vectors = {"x": np.array([2.0, 0.0]), "y": np.array([0.0, 3.0]), "z": np.array([1.0, 1.0])}

    cents = centroids_from_vectors(events, vectors)

    assert set(cents) == {"a", "b"}  # 埋込の無い事象は含めない
    assert centroid_cos(cents, "a", "b") == pytest.approx(1.0)
    assert centroid_cos(cents, "a", "c") == 0.0


def test_load_rejects_a_model_trained_on_another_window(tmp_path: Path) -> None:
    path = tmp_path / "w.json"
    path.write_text(
        json.dumps(
            {
                "feature_names": list(FEATURE_NAMES),
                "threshold": 0.8,
                "window_days": 999,
                "trees": [_stump(0, 0.5, 0.0, 1.0)],
            }
        )
    )
    load_relation_model.cache_clear()

    assert load_relation_model(str(path)) is None
