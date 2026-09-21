"""深掘り選定の ML 前段 — LLM に渡す候補を絞る (2026-09-21)。

⭐ **置き換えではなく絞り込み**。同じモデルでも役割で成否が変わる:

| 使い方 | LLM の上位 20 の回収 |
|---|---|
| 置き換え (ML の上位 20 をそのまま採用) | **37%** |
| 前段 top-60 | 75% |
| **前段 top-90** | **90%** |
| 前段 top-120 | 98% |

⚠ 最初「上位 20 対 上位 20」で測って「蒸留は不成立」と結論しかけた。用途に合わない
評価だった (利用者の「しきい値を変えればいいのでは」で気づいた)。

⭐ 90% という数字は、比較対象の LLM 自身の自己一致 **70%** を上回る。同じ入力を 2 回
採点すると上位 20 の 30% が入れ替わるので、**top-90 の絞りによる損失はその揺らぎより
小さい** = 実用上は無損失。

⚠ **ML 単独の品質は未測定**。ここで測ったのは「LLM と同じものを選ぶか」だけで、
どちらが良いかは深掘りに審判ラベルが無いので判定できない。detect は 09-10 に
現行由来ラベルで ML を棄却し、審判ラベルに替えて AUC 0.84 が出て覆った。
深掘りはまだその手前 — **前段としてのみ使い、判断は LLM に残す**。

モデルは JSON (`config/models/deep_dive_model.json`、`scripts/train_deep_dive_ml.py`)。
列の対応は ``feature_names`` の一致で関門する (列ずれは load で None = ML を使わない)。
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog

from src.digest.db_filter import DigestCandidate
from src.digest.deep_dive_features import DD_FEATURE_NAMES, DeepDiveExtra, dd_feature_vector

_log = structlog.get_logger(__name__)

DEFAULT_MODEL_PATH = Path("config/models/deep_dive_model.json")
_PREFILTER_ENV = "DEEPDIVE_ML_PREFILTER"
#: LLM に渡す候補の件数。実測で top-90 が LLM の上位 20 の 90% を覆う (上記の表)。
#: 0 で無効 (= 絞らない、従来の挙動)。
PREFILTER_TOP_K_DEFAULT = 90

_model: dict[str, Any] | None = None
_loaded = False


def prefilter_top_k() -> int:
    """LLM 採点の前段で残す件数 (``DEEPDIVE_ML_PREFILTER``、0 = 絞らない)。"""
    raw = os.environ.get(_PREFILTER_ENV, str(PREFILTER_TOP_K_DEFAULT))
    try:
        return max(0, int(raw))
    except ValueError:
        return PREFILTER_TOP_K_DEFAULT


def load_model(path: Path = DEFAULT_MODEL_PATH) -> dict[str, Any] | None:
    """モデルを読む。**列がずれていたら None** (黙って別の特徴で採点しない)。"""
    global _model, _loaded
    if _loaded:
        return _model
    _loaded = True
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _log.info("deep_dive_ml_model_absent", error=type(exc).__name__)
        return None
    if list(raw.get("feature_names") or []) != list(DD_FEATURE_NAMES):
        _log.warning(
            "deep_dive_ml_feature_mismatch",
            expected=len(DD_FEATURE_NAMES),
            got=len(raw.get("feature_names") or []),
        )
        return None
    _model = raw
    return _model


def score(
    candidates: Sequence[DigestCandidate],
    *,
    summary_len: dict[str, int] | None = None,
    articles: dict[str, Any] | None = None,
) -> dict[str, float]:
    """候補ごとの予測 composite (純粋に近い関数。モデル不在なら空 dict)。"""
    m = load_model()
    if m is None or not candidates:
        return {}
    mean, scale, coef, b = m["mean"], m["scale"], m["coef"], float(m["intercept"])
    out: dict[str, float] = {}
    for c in candidates:
        a = (articles or {}).get(c.article_id)
        if a is None:
            continue
        extra = DeepDiveExtra(summary_len=(summary_len or {}).get(c.article_id, 0))
        v = dd_feature_vector(a, extra)
        z = sum(
            ((x - mu) / (sd if sd else 1.0)) * w
            for x, mu, sd, w in zip(v, mean, scale, coef, strict=True)
        )
        out[c.article_id] = z + b
    return out


def prefilter_select(scores: dict[str, float], *, top_k: int) -> list[str]:
    """予測順に上位 top_k 件の article_id (純粋関数)。top_k<=0 なら空 = 絞らない合図。"""
    if top_k <= 0:
        return []
    return [a for a, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]]
