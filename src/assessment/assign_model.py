"""台帳の割当判定モデル (学習済みランダムフォレストの numpy 推論、2026-09-24)。

⭐ sklearn を本番に持ち込まない — 学習はオフラインで行い、木を JSON に書き出してここで推論する
(群化の ``eventnews.pair_model`` と同じ方式)。モデルが無い・壊れている・特徴量の並びが違う
ときは None を返し、呼び手は埋込の関門 (``assign_gate``) へ退避する。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_MODEL_PATH = Path("config/models/assign_model.json")
_ENABLE_ENV = "ASSIGN_ML"


def assign_ml_enabled() -> bool:
    """割当の判定に ML を使うか (``ASSIGN_ML=1``)。モデルが無ければ使えない。"""
    return os.environ.get(_ENABLE_ENV, "0").strip() == "1"


@dataclass(frozen=True)
class AssignModel:
    feature_names: tuple[str, ...]
    threshold: float
    _trees: tuple[dict[str, list[float]], ...]

    def probability(self, features: list[float]) -> float:
        """同じ情勢である確率 (木ごとの葉の比率の平均)。"""
        # ⚠ float32 で比較する (sklearn は入力を float32 に落として分岐する)
        x = np.asarray(features, dtype=np.float32)
        if x.shape[0] != len(self.feature_names):
            raise ValueError(f"特徴量の数が合いません: {x.shape[0]} != {len(self.feature_names)}")
        total = 0.0
        for tree in self._trees:
            left, right = tree["left"], tree["right"]
            feature, threshold, proba = tree["feature"], tree["threshold"], tree["proba"]
            node = 0
            while int(left[node]) != -1:
                idx = int(feature[node])
                node = int(left[node]) if x[idx] <= threshold[node] else int(right[node])
            total += float(proba[node])
        return total / len(self._trees)

    def assigns(self, features: list[float]) -> bool:
        return self.probability(features) >= self.threshold


@lru_cache(maxsize=2)
def load_assign_model(path: str | None = None) -> AssignModel | None:
    """モデルを読む。無い・壊れている・並びが違う → None (埋込の関門へ退避)。"""
    target = Path(path) if path else DEFAULT_MODEL_PATH
    if not target.exists():
        return None
    try:
        from src.assessment.assign_features import FEATURE_NAMES

        raw = json.loads(target.read_text(encoding="utf-8"))
        got = tuple(raw["feature_names"])
        if got != FEATURE_NAMES:
            _log.warning("assign_model_feature_mismatch", expected=len(FEATURE_NAMES), got=len(got))
            return None
        return AssignModel(
            feature_names=got, threshold=float(raw["threshold"]), _trees=tuple(raw["trees"])
        )
    except Exception as e:  # noqa: BLE001 — 壊れたモデルで台帳を止めない
        _log.warning("assign_model_load_failed", path=str(target), error=str(e)[:200])
        return None
