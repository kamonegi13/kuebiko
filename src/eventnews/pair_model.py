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
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.logging_config import get_logger
from src.tools.tree_forest import forest_probability

_log = get_logger(__name__)

DEFAULT_MODEL_PATH = Path("config/models/pair_model.json")
#: 判定器の照合 (``PAIR_MODEL_CHECK``)。"warn" (既定) = 食い違いを警告するだけで判定は変えない。
#: "off" = 照合しない。
PAIR_MODEL_CHECK_ENV = "PAIR_MODEL_CHECK"

#: pair の LLM 判定の旗。"1" (既定) = 従来どおり LLM を呼ぶ。"0" = 呼ばず、``llm_same`` /
#: ``llm_known`` を欠け (0, 0) にして ``threshold_llm_off`` で判定する (ML は動く)。
PAIR_LLM_ENV = "EVENTNEWS_PAIR_LLM"


def is_llm_enabled() -> bool:
    """旗が "0" のときだけ False。未設定・他の値は従来どおり (LLM あり)。"""
    return os.environ.get(PAIR_LLM_ENV, "1").strip() != "0"


@dataclass(frozen=True)
class PairModel:
    """学習済みモデル。``feature_names`` の順で値を渡すこと。"""

    feature_names: tuple[str, ...]
    threshold_llm_on: float
    threshold_llm_off: float
    _trees: tuple[dict[str, list[float]], ...]
    #: このモデルの特徴量 (llm_same) を作った判定器。空 = 不明 (照合しない)。
    trained_llm_models: tuple[str, ...] = ()

    def probability(self, features: list[float]) -> float:
        """同じ出来事である確率 (木ごとの葉の比率を平均する)。"""
        if len(features) != len(self.feature_names):
            raise ValueError(
                f"特徴量の数が合いません: {len(features)} != {len(self.feature_names)}"
            )
        return forest_probability(self._trees, features)

    def joins(self, features: list[float], *, llm_available: bool) -> bool:
        """繋ぐか。⭐ **閾値は LLM の有無で使い分ける** (欠測時は分布が変わる)。"""
        threshold = self.threshold_llm_on if llm_available else self.threshold_llm_off
        return self.probability(features) >= threshold


def _trained_llm_models(raw: dict[str, object]) -> tuple[str, ...]:
    """JSON の ``trained_llm_models`` (文字列の配列。無い・形が違う = 空 = 不明)。"""
    declared = raw.get("trained_llm_models")
    if not isinstance(declared, list):
        return ()
    return tuple(m.strip() for m in declared if isinstance(m, str) and m.strip())


def pair_llm_mismatch(model: PairModel, actual: str) -> bool:
    """特徴量を作った判定器の宣言があり、実モデルがその中に無いとき True (空宣言は False)。"""
    return bool(model.trained_llm_models) and actual.strip() not in model.trained_llm_models


def warn_on_pair_llm_mismatch(model: PairModel) -> None:
    """``step:pair_judge`` の実モデルとの食い違いを警告する。判定は変えない (warn のみ)。"""
    if os.environ.get(PAIR_MODEL_CHECK_ENV, "warn").strip().lower() == "off":
        return
    if not is_llm_enabled():  # LLM を使わない構成では pair_judge の実モデルは判定に効かない
        return
    from src.tools.model_tiers import Step, resolve_step_model

    try:
        actual = resolve_step_model(Step.PAIR_JUDGE)
    except Exception as e:  # noqa: BLE001 — 照合の失敗で群化を止めない
        _log.warning("pair_model_llm_resolve_failed", error=str(e)[:200])
        return
    if pair_llm_mismatch(model, actual):
        _log.warning(
            "pair_model_llm_mismatch", trained=list(model.trained_llm_models), actual=actual
        )


@lru_cache(maxsize=2)
def load_model(path: str | None = None) -> PairModel | None:
    """モデルを読む。**無ければ None** (呼び手は決定論へ退避する)。"""
    target = Path(path) if path else DEFAULT_MODEL_PATH
    if not target.exists():
        _log.warning("pair_model_missing", path=str(target))
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        from src.eventnews.pair_features import FEATURE_NAMES

        expected = tuple(FEATURE_NAMES) + ("llm_same", "llm_known")
        got = tuple(raw["feature_names"])
        if got != expected:
            # ⚠ 特徴の並びが違うモデルを黙って使うと、木が別の列を読んで**静かに
            #    誤判定**する (2026-09-03 に種別対 4 特徴を足した際の防御)。
            #    不一致は None = 決定論へ退避し、ログで気付けるようにする。
            _log.warning(
                "pair_model_feature_mismatch",
                expected=len(expected),
                got=len(got),
                first_diff=next(
                    (i for i, (a, b) in enumerate(zip(expected, got, strict=False)) if a != b), -1
                ),
            )
            return None
        model = PairModel(
            feature_names=got,
            threshold_llm_on=float(raw["threshold_llm_on"]),
            threshold_llm_off=float(raw["threshold_llm_off"]),
            _trees=tuple(raw["trees"]),
            trained_llm_models=_trained_llm_models(raw),
        )
        warn_on_pair_llm_mismatch(model)
        return model
    except Exception as e:  # noqa: BLE001 — 壊れたモデルで群化を止めない
        _log.warning("pair_model_load_failed", path=str(target), error=str(e)[:200])
        return None
