"""JSON に書き出したランダムフォレスト (sklearn) の numpy 推論の共通部品。

台帳の割当 (``AssignModel``)・群化 (``PairModel``)・深刻さ C が共用する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

_LEAF = -1  # sklearn の children_left は葉で -1


def forest_probability(
    trees: Sequence[Mapping[str, Sequence[float]]], features: Sequence[float]
) -> float:
    """木ごとの葉の比率の平均。

    ⚠ 特徴量は float32 に丸めるが、**閾値は float64 のまま比べる** (sklearn と同じ)。値を
    ``np.float32`` のまま Python の float と比べると NumPy 2 は閾値を float32 に丸め、
    閾値すれすれの値で分岐が反転する (実測で確率が最大 0.004 ずれた)。
    """
    x = [float(v) for v in np.asarray(features, dtype=np.float32)]
    total = 0.0
    for tree in trees:
        left, right = tree["left"], tree["right"]
        feature, threshold, proba = tree["feature"], tree["threshold"], tree["proba"]
        node = 0
        while int(left[node]) != _LEAF:
            go_left = x[int(feature[node])] <= threshold[node]
            node = int(left[node]) if go_left else int(right[node])
        total += float(proba[node])
    return total / len(trees)
