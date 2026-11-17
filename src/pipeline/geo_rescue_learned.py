# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""地政学の救済の学習版 (GEO_RESCUE_LEARNED、2026-11-17、**shadow のみ**)。

本番の救済 v2 (pir の判定 → 問いが閾値以上、手で組んだ順の関門) と並べて、特徴を組み合わせた小さな LR
(埋め込み + 手掛かり・pir/flat・問い QW/QC/QD の確率) の判定を**記録するだけ**。本番の救済・
analysis_only・配信は変えない。学習は ``scripts/train_geo_rescue_model.py``、係数は
``config/models/geo_rescue_model.json``。

このモジュールは**特徴量の組み立て (学習と推論で共通)・JSON の読み込み・確率**だけを持つ純粋な部品
(numpy のみ。sklearn に依存しない)。配線は ``geo_rescue_shadow.run_learned_shadow``。

特徴量の並びは ``FEATURE_NAMES`` が SSoT。JSON の ``feature_names`` が一致しなければ読まない
(学習と推論の食い違いで黙って誤った確率を出さないため)。
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import ArrayLike

from src.logging_config import get_logger
from src.pipeline.geo_rescue_cues import is_cyber_geo_cue

_log = get_logger(__name__)

FLAG_ENV = "GEO_RESCUE_LEARNED"
DEFAULT_MODEL_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "models" / "geo_rescue_model.json"
)
#: 手掛かりの種類 (``geo_rescue_cues`` の ``<種類>:<値>`` の先頭)。学習と推論で同じ並び
CUE_KINDS: tuple[str, ...] = ("nation", "war", "framework", "theatre", "trigger", "proxy")
FEATURE_NAMES: tuple[str, ...] = (
    "pir",
    "flat",
    "n_cues",
    "has_cyber_geo_cue",
    *(f"cue_{k}" for k in CUE_KINDS),
    "qw",
    "qc",
    "qd",
    "is_summary",
)
_LEVEL = {"low": 0.0, "medium": 1.0, "high": 2.0}
#: 埋め込みの入力の字数 (学習時と同じ。``OllamaEmbeddingClient`` の既定の上限とも同じ)
EMBED_INPUT_CHARS = 2000
MODEL_VERSION = 1


def learned_mode() -> str:
    """``GEO_RESCUE_LEARNED``: ``"shadow"`` だけを受け付け、他 (未設定・0・1) は ``"0"``。"""
    return "shadow" if os.environ.get(FLAG_ENV, "0").strip().lower() == "shadow" else "0"


def embedding_input(title: str | None, text: str | None) -> str:
    """埋め込みに渡す文字列 (学習のスクリプトと同じ: 題 + 改行 + 本文プレビュー、2,000 字まで)。"""
    return ((title or "") + "\n" + (text or ""))[:EMBED_INPUT_CHARS]


def build_features(
    *,
    pir: str | None,
    flat: str | None,
    cues: Sequence[str],
    p_qw: float | None,
    p_qc: float | None,
    p_qd: float | None,
    is_summary: bool,
) -> tuple[float, ...]:
    """``FEATURE_NAMES`` の順の特徴量。問いが無い (None) は 0、判定が error・未知は 0 (low 扱い)。"""
    values = (
        _LEVEL.get(pir or "", 0.0),
        _LEVEL.get(flat or "", 0.0),
        float(len(cues)),
        float(any(is_cyber_geo_cue(c) for c in cues)),
        *(float(any(c.startswith(k) for c in cues)) for k in CUE_KINDS),
        p_qw or 0.0,
        p_qc or 0.0,
        p_qd or 0.0,
        float(is_summary),
    )
    return tuple(float(v) for v in values)


@dataclass(frozen=True)
class LearnedModel:
    """学習済みの LR (係数は [埋め込み | 特徴量 ÷ scale] の順)。"""

    scale: tuple[float, ...]
    coef: tuple[float, ...]
    intercept: float
    threshold: float
    embedding_dim: int
    probe_model: str
    embedding_model: str
    cv_recall: float | None
    cv_false_rescue: float | None


def _validate(raw: dict[str, Any]) -> LearnedModel:
    if raw.get("version") != MODEL_VERSION:
        raise ValueError("version")
    if tuple(raw["feature_names"]) != FEATURE_NAMES:
        raise ValueError("feature_names")
    dim = int(raw["embedding_dim"])
    scale = tuple(float(x) for x in raw["scale"])
    coef = tuple(float(x) for x in raw["coef"])
    if len(scale) != len(FEATURE_NAMES) or len(coef) != dim + len(FEATURE_NAMES) or dim <= 0:
        raise ValueError("length")
    if any(s <= 0 for s in scale):
        raise ValueError("scale")
    cv = raw.get("cv") or {}
    return LearnedModel(
        scale=scale,
        coef=coef,
        intercept=float(raw["intercept"]),
        threshold=float(raw["threshold"]),
        embedding_dim=dim,
        probe_model=str(raw["probe_model"]),
        embedding_model=str(raw["embedding_model"]),
        cv_recall=None if cv.get("recall") is None else float(cv["recall"]),
        cv_false_rescue=None if cv.get("false_rescue") is None else float(cv["false_rescue"]),
    )


def load_model(path: Path | None = None) -> LearnedModel | None:
    """JSON から読む。無い・壊れている・特徴量の並びが違うなら ``None`` (警告ログ。例外は出さない)。"""
    p = path or DEFAULT_MODEL_PATH
    if not p.exists():
        _log.warning("geo_rescue_model_missing", path=p.name)
        return None
    try:
        return _validate(json.loads(p.read_text(encoding="utf-8")))
    except (ValueError, KeyError, TypeError, OSError) as e:
        _log.warning("geo_rescue_model_invalid", path=p.name, error=type(e).__name__)
        return None


def predict_proba(model: LearnedModel, embedding: ArrayLike, features: Sequence[float]) -> float:
    """救済の確率 P(救う)。``embedding`` は ``embedding_dim`` 次元、``features`` は ``FEATURE_NAMES`` の順。"""
    emb = np.asarray(embedding, dtype=float)
    if emb.shape != (model.embedding_dim,) or len(features) != len(FEATURE_NAMES):
        raise ValueError("次元が学習時と違う")
    x = np.concatenate([emb, np.asarray(features) / model.scale])
    z = float(np.dot(np.asarray(model.coef), x) + model.intercept)
    return 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))


__all__ = [
    "CUE_KINDS",
    "DEFAULT_MODEL_PATH",
    "EMBED_INPUT_CHARS",
    "FEATURE_NAMES",
    "FLAG_ENV",
    "LearnedModel",
    "build_features",
    "embedding_input",
    "learned_mode",
    "load_model",
    "predict_proba",
]
