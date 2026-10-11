"""重複 alert の ML (学習済み木の numpy 推論、R1h、2026-10-11)。

学習は scripts/train_alert_dedup_model.py (Opus 正解の alert の組)。木の形式は
``pair_model.json`` と同じ。推論は ``PairModel`` の木の辿り方を流用する。
目的変数 = suppress (抑止すべき重複)。モデルが無い・壊れている・特徴の並びが違うときは None。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.cti.alert_dedup_features import FEATURE_NAMES
from src.eventnews.pair_model import PairModel
from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_MODEL_PATH = Path("config/models/alert_dedup_model.json")
#: 閾値の既定 (model json に ``threshold`` が無いとき)
DEFAULT_THRESHOLD = 0.5


@dataclass(frozen=True)
class AlertDedupModel:
    version: str
    threshold: float
    _inner: PairModel

    def p_suppress(self, features: list[float]) -> float:
        """抑止すべき重複である確率。"""
        return self._inner.probability(features)

    def would_suppress(self, p: float) -> bool:
        return p >= self.threshold


@lru_cache(maxsize=2)
def load_alert_dedup_model(path: str | None = None) -> AlertDedupModel | None:
    target = Path(path) if path else DEFAULT_MODEL_PATH
    if not target.exists():
        _log.warning("alert_dedup_model_missing", path=str(target))
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        got = tuple(raw["feature_names"])
        if got != FEATURE_NAMES:
            # ⚠ 並びの違うモデルは木が別の列を読んで静かに誤判定する
            _log.warning(
                "alert_dedup_model_feature_mismatch", expected=len(FEATURE_NAMES), got=len(got)
            )
            return None
        threshold = float(raw.get("threshold", DEFAULT_THRESHOLD))
        inner = PairModel(
            feature_names=got,
            threshold_llm_on=threshold,
            threshold_llm_off=threshold,
            _trees=tuple(raw["trees"]),
        )
        return AlertDedupModel(str(raw.get("version", "")), threshold, inner)
    except Exception as e:  # noqa: BLE001 — 壊れたモデルで配信を止めない
        _log.warning("alert_dedup_model_load_failed", path=str(target), error=str(e)[:200])
        return None


__all__ = ["DEFAULT_MODEL_PATH", "AlertDedupModel", "load_alert_dedup_model"]
