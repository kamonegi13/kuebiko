"""深掘り選定の特徴量 (2026-09-20)。

detect の特徴量 (`detect_features.FEATURE_NAMES`) を**そのまま再利用**し、深掘り固有の
列だけを足す。detect 側の列順は既存モデルの契約なので触らない (列ずれ検証が守っている)。

⚠ 決定論 composite (`db_filter._deterministic_composite`) 自体は特徴に入れない。
構成要素 (報道の広がり・PIR 件数・actor 帰属・KEV) は既に列としてあり、合計値を足すと
**現行の関門をそのまま学習する**方向へ引っ張られる。関門が何を落としているかを見たいので、
現行の判断を教師の入力に混ぜない (09-04「現状を真値に置くバイアス」)。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.synthesis.grounded.detect_features import (
    FEATURE_NAMES as _DETECT_FEATURE_NAMES,
)
from src.synthesis.grounded.detect_features import (
    DetectArticle,
    feature_vector,
)

#: 深掘り固有の列。
#: - corroboration: 同一 dedup_key の報道本数。**重要性の代理ではない**が、rubric の
#:   判断材料ではあるので特徴として渡す (関門のように足切りには使わない)
#: - age_hours: 窓終端からの経過時間 (rubric の timeliness 軸に対応)
#: - is_novel: 過去 4 週に同じ dedup_key が選定されていないか (novelty 軸)
#: - summary_len: 要約の厚み (ROI 軸の粗い代理)
#: - title_has_org: 組織名らしき語を含むか (被害組織が具体的な記事は深掘り価値が高い)
DEEP_DIVE_EXTRA: tuple[str, ...] = (
    "corroboration",
    "age_hours",
    "is_novel",
    "summary_len",
)

DD_FEATURE_NAMES: tuple[str, ...] = _DETECT_FEATURE_NAMES + DEEP_DIVE_EXTRA


@dataclass(frozen=True)
class DeepDiveExtra:
    """深掘り固有の入力 (detect 側の DetectArticle に足す分)。"""

    corroboration: int = 1
    age_hours: float = 0.0
    is_novel: bool = True
    summary_len: int = 0


def dd_feature_vector(a: DetectArticle, extra: DeepDiveExtra) -> list[float]:
    """記事 1 件 → 特徴量ベクトル。順序は DD_FEATURE_NAMES と 1:1 (テストが固定する)。"""
    return feature_vector(a) + [
        float(extra.corroboration),
        float(extra.age_hours),
        1.0 if extra.is_novel else 0.0,
        float(extra.summary_len),
    ]
