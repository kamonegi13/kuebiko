"""記事ペアの判定モデル (学習済みランダムフォレストの推論)。

⭐ **sklearn を本番に持ち込まない。** 学習はオフラインで行い、木を JSON へ書き出して
ここで numpy だけで推論する。依存を増やさず、推論は数ミリ秒で済む
(CLAUDE.md §7「依存追加は最小化」)。

2026-08-31 の実測 (365 組・5-fold):
- LLM の判定あり: **89%** (誤結合 9 以下の条件)
- LLM が落ちている: **85%** — ⭐ 学習時に 30% を「不明」として伏せてあるため、
  欠測でも劣化が小さい。伏せずに学習すると 83% に沈む
  (``llm_same=0`` を「別だと判定された」と誤読するため)
- 決定論の規則のみ (現行): 76%
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_MODEL_PATH = Path("config/models/pair_model.json")


@dataclass(frozen=True)
class PairModel:
    """学習済みモデル。``feature_names`` の順で値を渡すこと。"""

    feature_names: tuple[str, ...]
    threshold_llm_on: float
    threshold_llm_off: float
    _trees: tuple[dict[str, list[float]], ...]

    def probability(self, features: list[float]) -> float:
        """同じ出来事である確率 (木ごとの葉の比率を平均する)。"""
        # ⚠ **float32 で比較する。** sklearn は内部で入力を float32 に落とすので、
        #    float64 のまま比べると閾値の近傍で分岐が変わる (実測 最大差 0.0034)。
        x = np.asarray(features, dtype=np.float32)
        if x.shape[0] != len(self.feature_names):
            raise ValueError(f"特徴量の数が合いません: {x.shape[0]} != {len(self.feature_names)}")
        total = 0.0
        for tree in self._trees:
            left, right = tree["left"], tree["right"]
            feature, threshold, proba = tree["feature"], tree["threshold"], tree["proba"]
            node = 0
            while int(left[node]) != -1:  # 葉は children_left == -1
                idx = int(feature[node])
                node = int(left[node]) if x[idx] <= threshold[node] else int(right[node])
            total += float(proba[node])
        return total / len(self._trees)

    def joins(self, features: list[float], *, llm_available: bool) -> bool:
        """繋ぐか。⭐ **閾値は LLM の有無で使い分ける** (欠測時は分布が変わる)。"""
        threshold = self.threshold_llm_on if llm_available else self.threshold_llm_off
        return self.probability(features) >= threshold


@lru_cache(maxsize=2)
def load_model(path: str | None = None) -> PairModel | None:
    """モデルを読む。**無ければ None** (呼び手は決定論へ退避する)。"""
    target = Path(path) if path else DEFAULT_MODEL_PATH
    if not target.exists():
        _log.warning("pair_model_missing", path=str(target))
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return PairModel(
            feature_names=tuple(raw["feature_names"]),
            threshold_llm_on=float(raw["threshold_llm_on"]),
            threshold_llm_off=float(raw["threshold_llm_off"]),
            _trees=tuple(raw["trees"]),
        )
    except Exception as e:  # noqa: BLE001 — 壊れたモデルで群化を止めない
        _log.warning("pair_model_load_failed", path=str(target), error=str(e)[:200])
        return None
