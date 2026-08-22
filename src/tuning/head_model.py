"""古典 ML 蒸留ヘッド v0 の学習・推論・成果物 IO (ロードマップ C、設計は
``docs/self_evolving_tuning_design.md`` §14)。

記事 embedding (article_embeddings.vector) を入力に、summarizer の最終判定
(articles.importance / articles.category) を LogisticRegression で蒸留する。
本 module は **純ロジックのみ** — DB / ファイル I/O は成果物の保存 (``save_artifact``)
と読込 (``load_artifact``) に限定し、学習データの取得は呼出側 (``scripts/train_head.py``)
の責務とする。

較正確率 (predict_proba) がこのヘッドの価値の核心なので、``class_weight`` は
指定しない — base rate を歪めた確率は能動学習の不確実性サンプリングに使えない。

``src/tuning/head_shadow.py`` (§14.3 のシャドー配線) が本 module の
``HeadBundle`` / ``HeadMetadata`` / ``load_artifact`` / ``predict_batch`` を
既に消費している。**関数名・シグネチャはそちら側の実消費契約が SSoT** —
本 docstring の記述もそれに合わせてある。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import sklearn
from numpy.typing import NDArray
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score, precision_recall_fscore_support

# ===== 定数 (マジックナンバー排除) =====

_LOGREG_MAX_ITER = 2000
# category 分類器の学習対象クラスに要求する最小学習標本数。
_CATEGORY_MIN_TRAIN_SAMPLES = 100
# category モデルを学習するために必要な最小クラス数 (2 未満は分類として無意味)。
_MIN_CATEGORY_CLASSES = 2
_ECE_DEFAULT_BINS = 10
_IMPORTANCE_HIGH = "high"
_IMPORTANCE_LOW = "low"

_MODEL_FILENAME = "model.joblib"
_METADATA_FILENAME = "metadata.json"


class HeadArtifactMismatchError(Exception):
    """成果物の embedding_model / dim が呼出側の期待と一致しないときに送出する。

    呼出側 (``head_shadow.py`` 等) は fail-open 前提でこの例外を広く捕捉する。
    """


@dataclass(frozen=True)
class HeadBundle:
    """学習済み分類器一式。importance は必須、category は学習不能なら None。"""

    importance_model: LogisticRegression
    importance_classes: tuple[str, ...]
    category_model: LogisticRegression | None
    category_classes: tuple[str, ...]


@dataclass(frozen=True)
class HeadMetadata:
    """成果物のメタデータ (人間可読 JSON で保存)。

    ``metrics`` は JSON 化可能な素朴な型 (str/int/float/bool/list/dict/None) のみを
    格納する契約 — 呼出側で任意オブジェクトを詰めない。
    """

    embedding_model: str
    dim: int
    artifact_version: str
    trained_at: str
    n_train: int
    n_eval: int
    importance_classes: tuple[str, ...]
    category_classes: tuple[str, ...]
    metrics: dict[str, Any]
    rubric_version: str | None
    sklearn_version: str


@dataclass(frozen=True)
class HeadPrediction:
    """1 記事分の推論結果。"""

    importance: str
    importance_probs: dict[str, float]
    category: str | None
    category_prob: float | None


# ===== ECE (Expected Calibration Error) =====


def ece(
    confidences: Sequence[float],
    correct: Sequence[bool],
    n_bins: int = _ECE_DEFAULT_BINS,
) -> float:
    """top-1 確信度の Expected Calibration Error (10-bin 既定)。

    ``confidences[i]`` は i 件目の予測確率 (top-1)、``correct[i]`` はその予測が
    正解だったか。等幅 bin ごとに |平均正解率 - 平均確信度| を標本数で重み付けして
    合算する。標本 0 件は 0.0 を返す (較正の主張ができないだけで、誤りとは扱わない)。
    """
    conf = np.asarray(confidences, dtype=np.float64)
    corr = np.asarray(correct, dtype=np.float64)
    if conf.shape[0] == 0:
        return 0.0
    if conf.shape != corr.shape:
        raise ValueError("confidences と correct は同じ長さが必要")

    n = conf.shape[0]
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    last_edge = edges[-1]
    total_gap = 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        mask = (conf >= lo) & (conf <= hi if hi == last_edge else conf < hi)
        bin_n = int(mask.sum())
        if bin_n == 0:
            continue
        bin_acc = float(corr[mask].mean())
        bin_conf = float(conf[mask].mean())
        total_gap += (bin_n / n) * abs(bin_acc - bin_conf)
    return total_gap


# ===== 検証ヘルパ =====


def _as_float32_matrix(x: NDArray[np.float32], name: str) -> NDArray[np.float32]:
    arr = np.asarray(x, dtype=np.float32)
    if arr.ndim != 2:
        raise ValueError(f"{name} は 2 次元配列である必要がある (got {arr.ndim}d)")
    return arr


def _validate_dim(x: NDArray[np.float32], dim: int, name: str) -> None:
    if x.shape[1] != dim:
        raise ValueError(f"{name} の次元数が dim と不一致 (got {x.shape[1]}, expected {dim})")


def _validate_row_count(x: NDArray[np.float32], labels: Sequence[Any], name: str) -> None:
    if len(labels) != x.shape[0]:
        raise ValueError(f"{name} の行数が X と不一致 (got {len(labels)}, expected {x.shape[0]})")


# ===== 学習 =====


def _evaluate_importance(
    model: LogisticRegression,
    x_eval: NDArray[np.float32],
    y_eval: NDArray[np.object_],
    classes: tuple[str, ...],
) -> dict[str, Any]:
    """importance 分類器の eval 指標 (accuracy / per-class / macro-F1 / ECE / 致命混同)。"""
    proba = model.predict_proba(x_eval)
    pred_idx = np.argmax(proba, axis=1)
    y_pred = np.asarray([classes[i] for i in pred_idx], dtype=object)
    y_true = np.asarray([str(v) for v in y_eval], dtype=object)
    correct = y_pred == y_true
    confidences = proba[np.arange(proba.shape[0]), pred_idx]

    precision, recall, _f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=list(classes), zero_division=0
    )
    macro_f1 = float(
        f1_score(y_true, y_pred, labels=list(classes), average="macro", zero_division=0)
    )
    per_class = {
        cls: {
            "precision": float(precision[i]),
            "recall": float(recall[i]),
            "support": int(support[i]),
        }
        for i, cls in enumerate(classes)
    }

    high_mask = y_true == _IMPORTANCE_HIGH
    high_to_low = int(np.sum(high_mask & (y_pred == _IMPORTANCE_LOW)))
    high_total = int(high_mask.sum())
    high_to_low_rate = (high_to_low / high_total) if high_total > 0 else 0.0

    return {
        "accuracy": float(correct.mean()),
        "macro_f1": macro_f1,
        "ece": ece(confidences.tolist(), correct.tolist()),
        "per_class": per_class,
        "critical_confusion": {
            "high_to_low_count": high_to_low,
            "high_to_low_rate": high_to_low_rate,
        },
    }


def _train_and_eval_category(
    x_train: NDArray[np.float32],
    y_cat_train: Sequence[str | None],
    x_eval: NDArray[np.float32],
    y_cat_eval: Sequence[str | None],
) -> tuple[LogisticRegression | None, tuple[str, ...], dict[str, Any]]:
    """100 件以上のクラスのみで category 分類器を学習する (満たなければ学習しない)。"""
    counts = Counter(c for c in y_cat_train if c)
    qualifying = frozenset(c for c, n in counts.items() if n >= _CATEGORY_MIN_TRAIN_SAMPLES)

    empty_metrics = {"top1_accuracy": 0.0, "coverage": 0.0, "trained_classes": 0}
    if len(qualifying) < _MIN_CATEGORY_CLASSES:
        return None, (), empty_metrics

    train_mask = np.asarray([c in qualifying for c in y_cat_train])
    x_cat_train = x_train[train_mask]
    y_cat_train_arr = np.asarray([c for c in y_cat_train if c in qualifying], dtype=object)

    model = LogisticRegression(max_iter=_LOGREG_MAX_ITER)
    model.fit(x_cat_train, y_cat_train_arr)
    category_classes = tuple(str(c) for c in model.classes_)

    eval_mask = np.asarray([c in qualifying for c in y_cat_eval])
    coverage = float(eval_mask.mean()) if len(y_cat_eval) > 0 else 0.0
    if int(eval_mask.sum()) == 0:
        accuracy = 0.0
    else:
        x_cat_eval = x_eval[eval_mask]
        y_cat_eval_arr = np.asarray([c for c in y_cat_eval if c in qualifying], dtype=object)
        pred = model.predict(x_cat_eval)
        accuracy = float(np.mean(pred == y_cat_eval_arr))

    metrics = {
        "top1_accuracy": accuracy,
        "coverage": coverage,
        "trained_classes": len(category_classes),
    }
    return model, category_classes, metrics


def _hash_metadata_core(core: dict[str, Any]) -> str:
    """メタデータ本体 (artifact_version 自身を除く) の sha256 先頭 12 桁。"""
    encoded = json.dumps(core, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def train_head(
    x_train: NDArray[np.float32],
    y_imp_train: Sequence[str],
    y_cat_train: Sequence[str | None],
    x_eval: NDArray[np.float32],
    y_imp_eval: Sequence[str],
    y_cat_eval: Sequence[str | None],
    *,
    embedding_model: str,
    dim: int,
    trained_at: str,
    rubric_version: str | None = None,
) -> tuple[HeadBundle, HeadMetadata]:
    """importance (全行) + category (100 件以上のクラスのみ) の 2 分類器を学習する。

    ``trained_at`` は呼出側が渡す ISO 時刻文字列 (本関数は現在時刻を暗黙取得しない)。
    """
    x_train = _as_float32_matrix(x_train, "X_train")
    x_eval = _as_float32_matrix(x_eval, "X_eval")
    _validate_dim(x_train, dim, "X_train")
    _validate_dim(x_eval, dim, "X_eval")
    _validate_row_count(x_train, y_imp_train, "y_imp_train")
    _validate_row_count(x_train, y_cat_train, "y_cat_train")
    _validate_row_count(x_eval, y_imp_eval, "y_imp_eval")
    _validate_row_count(x_eval, y_cat_eval, "y_cat_eval")
    if x_train.shape[0] == 0:
        raise ValueError("X_train が空")
    if x_eval.shape[0] == 0:
        raise ValueError("X_eval が空")

    y_imp_train_arr = np.asarray(list(y_imp_train), dtype=object)
    y_imp_eval_arr = np.asarray(list(y_imp_eval), dtype=object)

    # class_weight は指定しない — base rate を反映した較正確率がこのヘッドの価値。
    # lbfgs solver は 3 クラス以上で自動的に multinomial 損失を最適化する。
    imp_model = LogisticRegression(max_iter=_LOGREG_MAX_ITER)
    imp_model.fit(x_train, y_imp_train_arr)
    importance_classes = tuple(str(c) for c in imp_model.classes_)

    imp_metrics = _evaluate_importance(imp_model, x_eval, y_imp_eval_arr, importance_classes)
    cat_model, category_classes, cat_metrics = _train_and_eval_category(
        x_train, y_cat_train, x_eval, y_cat_eval
    )

    metrics: dict[str, Any] = {"importance": imp_metrics, "category": cat_metrics}
    n_train = int(x_train.shape[0])
    n_eval = int(x_eval.shape[0])
    meta_core = {
        "embedding_model": embedding_model,
        "dim": dim,
        "trained_at": trained_at,
        "n_train": n_train,
        "n_eval": n_eval,
        "importance_classes": list(importance_classes),
        "category_classes": list(category_classes),
        "metrics": metrics,
        "rubric_version": rubric_version,
        "sklearn_version": sklearn.__version__,
    }
    artifact_version = _hash_metadata_core(meta_core)

    metadata = HeadMetadata(
        embedding_model=embedding_model,
        dim=dim,
        artifact_version=artifact_version,
        trained_at=trained_at,
        n_train=n_train,
        n_eval=n_eval,
        importance_classes=importance_classes,
        category_classes=category_classes,
        metrics=metrics,
        rubric_version=rubric_version,
        sklearn_version=sklearn.__version__,
    )
    bundle = HeadBundle(
        importance_model=imp_model,
        importance_classes=importance_classes,
        category_model=cat_model,
        category_classes=category_classes,
    )
    return bundle, metadata


# ===== 推論 =====


def predict_batch(bundle: HeadBundle, vectors: NDArray[np.float32]) -> list[HeadPrediction]:
    """embedding 行列 (n, dim) をまとめて推論する。"""
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim != 2:
        raise ValueError(f"vectors は 2 次元配列である必要がある (got {arr.ndim}d)")

    imp_proba = bundle.importance_model.predict_proba(arr)
    imp_classes = [str(c) for c in bundle.importance_model.classes_]

    cat_proba: NDArray[np.float64] | None = None
    cat_classes: list[str] = []
    if bundle.category_model is not None:
        cat_proba = bundle.category_model.predict_proba(arr)
        cat_classes = [str(c) for c in bundle.category_model.classes_]

    predictions: list[HeadPrediction] = []
    for i in range(arr.shape[0]):
        row = imp_proba[i]
        top_idx = int(np.argmax(row))
        importance_probs = {cls: float(p) for cls, p in zip(imp_classes, row, strict=True)}

        category: str | None = None
        category_prob: float | None = None
        if cat_proba is not None:
            crow = cat_proba[i]
            c_idx = int(np.argmax(crow))
            category = cat_classes[c_idx]
            category_prob = float(crow[c_idx])

        predictions.append(
            HeadPrediction(
                importance=imp_classes[top_idx],
                importance_probs=importance_probs,
                category=category,
                category_prob=category_prob,
            )
        )
    return predictions


# ===== 成果物 IO =====


def save_artifact(directory: Path, bundle: HeadBundle, metadata: HeadMetadata) -> None:
    """model.joblib + metadata.json (indent=2, ensure_ascii=False) をディレクトリに保存する。"""
    directory.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, directory / _MODEL_FILENAME)
    payload = asdict(metadata)
    with (directory / _METADATA_FILENAME).open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_artifact(
    directory: Path,
    expected_embedding_model: str,
    expected_dim: int | None = None,
) -> tuple[HeadBundle, HeadMetadata]:
    """成果物を読み込み、embedding_model (必須) / dim (任意) を検証する。

    ``expected_dim`` は省略可 — ``src/tuning/head_shadow.py`` は per-vector で
    独自に dim 一致を確認するため embedding_model のみを渡す。両方を検証したい
    呼出側 (本 module のテスト等) は明示的に渡す。
    """
    metadata_path = directory / _METADATA_FILENAME
    model_path = directory / _MODEL_FILENAME
    if not metadata_path.is_file() or not model_path.is_file():
        raise HeadArtifactMismatchError(f"成果物が見つからない: {directory}")

    with metadata_path.open(encoding="utf-8") as f:
        raw: dict[str, Any] = json.load(f)

    metadata = HeadMetadata(
        embedding_model=str(raw["embedding_model"]),
        dim=int(raw["dim"]),
        artifact_version=str(raw["artifact_version"]),
        trained_at=str(raw["trained_at"]),
        n_train=int(raw["n_train"]),
        n_eval=int(raw["n_eval"]),
        importance_classes=tuple(raw["importance_classes"]),
        category_classes=tuple(raw["category_classes"]),
        metrics=raw["metrics"],
        rubric_version=raw.get("rubric_version"),
        sklearn_version=str(raw["sklearn_version"]),
    )

    if metadata.embedding_model != expected_embedding_model:
        raise HeadArtifactMismatchError(
            f"embedding_model 不一致: artifact={metadata.embedding_model} "
            f"expected={expected_embedding_model}"
        )
    if expected_dim is not None and metadata.dim != expected_dim:
        raise HeadArtifactMismatchError(
            f"dim 不一致: artifact={metadata.dim} expected={expected_dim}"
        )

    bundle = joblib.load(model_path)
    if not isinstance(bundle, HeadBundle):
        raise HeadArtifactMismatchError(f"{_MODEL_FILENAME} の中身が HeadBundle でない")
    return bundle, metadata
