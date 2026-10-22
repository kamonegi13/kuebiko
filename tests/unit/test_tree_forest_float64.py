"""木の閾値は float64 のまま比べる (NumPy 2 が閾値を float32 に丸めて分岐が反転する不具合の回帰)。

sklearn は入力だけを float32 に落とし、閾値 (float64) とは厳密に比べる。1.8749999 は
float32 では 1.875 に丸まるため、丸めた比較では特徴量 1.875 が「以下」と誤判定される。
"""

from __future__ import annotations

import numpy as np

from src.assessment.assign_model import AssignModel
from src.eventnews.pair_model import PairModel
from src.eventnews.relation_model import RelationModel
from src.tools.tree_forest import forest_probability

_TREE: dict[str, list[float]] = {
    "left": [1, -1, -1],
    "right": [2, -1, -1],
    "feature": [0, -2, -2],
    "threshold": [1.8749999, 0.0, 0.0],
    "proba": [0.5, 0.0, 1.0],
}  # fmt: skip


def test_forest_probability_compares_threshold_in_float64() -> None:
    assert forest_probability([_TREE], [1.875]) == 1.0
    assert forest_probability([_TREE], [1.8749999]) == 0.0


def test_forest_probability_averages_trees() -> None:
    leaf: dict[str, list[float]] = {
        "left": [-1], "right": [-1], "feature": [-2], "threshold": [0.0], "proba": [0.2],
    }  # fmt: skip
    other = {**leaf, "proba": [0.6]}
    assert abs(forest_probability([leaf, other], [0.0]) - 0.4) < 1e-12


def test_assign_model_matches_exact_float64_split() -> None:
    model = AssignModel(feature_names=("x",), threshold=0.5, _trees=(_TREE,))
    assert model.probability([1.875]) == 1.0
    assert model.assigns([1.875])


def test_pair_model_matches_exact_float64_split() -> None:
    model = PairModel(
        feature_names=("x",), threshold_llm_on=0.5, threshold_llm_off=0.5, _trees=(_TREE,)
    )
    assert model.probability([1.875]) == 1.0


def test_relation_model_matches_exact_float64_split() -> None:
    model = RelationModel(feature_names=("x",), threshold=0.5, _trees=(_TREE,))
    got = model.probabilities(np.array([[1.875], [1.8749999]]))
    assert got.tolist() == [1.0, 0.0]


def test_feature_count_mismatch_raises() -> None:
    import pytest

    model = AssignModel(feature_names=("x", "y"), threshold=0.5, _trees=(_TREE,))
    with pytest.raises(ValueError):
        model.probability([1.0])
