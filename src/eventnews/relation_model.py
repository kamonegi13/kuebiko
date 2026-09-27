"""事象どうしの関係の分類器 (学習済みランダムフォレストの numpy 推論、2026-09-27)。

判定するのは「同じ出来事の系統か」(続報・側面・包含 — 同じ事案・同じ脆弱性の別報道・まとめ)。
同じアクターは分類器ではなく、信頼できる経路の主題アクターの共有で決定論に決める (relations.py)。
学習は scripts/train_relation_model.py (Opus 盲検、定義 v3)。

⭐ sklearn を本番に持ち込まない (群化の pair_model・台帳の assign_model と同じ方式)。モデルが無い・
壊れている・特徴量の並びが違うときは None — 呼び手は「同じ出来事の系統」を出さない。
⭐ 候補は 2 万組あるので、推論は木ごとに全組をまとめて進める (1 組ずつだと 1 分を超える)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_MODEL_PATH = Path("config/models/relation_model.json")


@dataclass(frozen=True)
class RelationModel:
    feature_names: tuple[str, ...]
    threshold: float
    _trees: tuple[dict[str, list[float]], ...]

    def probabilities(self, features: np.ndarray) -> np.ndarray:
        """各組が同じ出来事の系統である確率 (木ごとの葉の比率の平均)。

        ``features`` は (組数, 特徴数)。
        """
        # ⚠ float32 で比較する (sklearn は入力を float32 に落として分岐する)
        x = np.asarray(features, dtype=np.float32)
        if x.ndim != 2 or x.shape[1] != len(self.feature_names):
            raise ValueError(f"特徴量の形が合いません: {x.shape} / {len(self.feature_names)} 列")
        rows = np.arange(x.shape[0])
        total = np.zeros(x.shape[0], dtype=np.float64)
        for tree in self._trees:
            left = np.asarray(tree["left"], dtype=np.int64)
            right = np.asarray(tree["right"], dtype=np.int64)
            feature = np.asarray(tree["feature"], dtype=np.int64)
            threshold = np.asarray(tree["threshold"], dtype=np.float64)
            node = np.zeros(x.shape[0], dtype=np.int64)
            active = left[node] != -1  # 葉は children_left == -1
            while active.any():
                n = node[active]
                go_left = x[rows[active], feature[n]] <= threshold[n]
                node[active] = np.where(go_left, left[n], right[n])
                active = left[node] != -1
            total += np.asarray(tree["proba"], dtype=np.float64)[node]
        return total / len(self._trees)


@lru_cache(maxsize=2)
def load_relation_model(path: str | None = None) -> RelationModel | None:
    """モデルを読む。無い・壊れている・並びが違う → None。"""
    target = Path(path) if path else DEFAULT_MODEL_PATH
    if not target.exists():
        return None
    try:
        from src.eventnews.relation_pair_features import FEATURE_NAMES

        raw = json.loads(target.read_text(encoding="utf-8"))
        got = tuple(raw["feature_names"])
        if got != FEATURE_NAMES:
            # ⚠ 並びの違うモデルを使うと木が別の列を読んで静かに誤判定する
            _log.warning(
                "relation_model_feature_mismatch", expected=len(FEATURE_NAMES), got=len(got)
            )
            return None
        from src.eventnews.relations import WINDOW_DAYS

        window = raw.get("window_days")
        if window is not None and int(window) != WINDOW_DAYS:
            # 珍しさ (df) の母集団が学習時と違う → 較正が静かにずれる
            _log.warning("relation_model_window_mismatch", trained=window, serving=WINDOW_DAYS)
            return None
        return RelationModel(
            feature_names=got, threshold=float(raw["threshold"]), _trees=tuple(raw["trees"])
        )
    except Exception as e:  # noqa: BLE001 — 壊れたモデルで事象画面を止めない
        _log.warning("relation_model_load_failed", path=str(target), error=str(e)[:200])
        return None
