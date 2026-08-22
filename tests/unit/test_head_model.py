"""古典 ML 蒸留ヘッド v0 (src.tuning.head_model) のユニットテスト。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from src.tuning.head_model import (
    HeadArtifactMismatchError,
    HeadBundle,
    HeadMetadata,
    ece,
    load_artifact,
    predict_batch,
    save_artifact,
    train_head,
)

_DIM = 6
_EMBED_MODEL = "test-embed-model"


def _make_synthetic_dataset(
    n_per_class: int = 40, dim: int = _DIM
) -> tuple[NDArray[np.float32], list[str], list[str | None]]:
    """3 クラスが線形分離可能な合成 embedding (クラスごとに異なる中心 + 小さいノイズ)。"""
    rng = np.random.default_rng(seed=42)
    centers = {
        "high": np.full(dim, 5.0, dtype=np.float32),
        "medium": np.zeros(dim, dtype=np.float32),
        "low": np.full(dim, -5.0, dtype=np.float32),
    }
    categories = {"high": "vuln", "medium": "malware", "low": "vuln"}
    vectors: list[NDArray[np.float32]] = []
    importances: list[str] = []
    cats: list[str | None] = []
    for imp, center in centers.items():
        noise = rng.normal(scale=0.05, size=(n_per_class, dim)).astype(np.float32)
        vectors.append(noise + center)
        importances.extend([imp] * n_per_class)
        cats.extend([categories[imp]] * n_per_class)
    x = np.vstack(vectors).astype(np.float32)
    return x, importances, cats


def _repeat_labels(pairs: Sequence[tuple[str, int]]) -> list[str | None]:
    """``[(label, count), ...]`` を展開した ``list[str | None]``。

    ``["a"] * n + ["b"] * m`` を ``list[str | None]`` 変数へ代入すると mypy の
    list invariance で弾かれるため、要素追加型のヘルパで組み立てる。
    """
    labels: list[str | None] = []
    for label, count in pairs:
        labels.extend([label] * count)
    return labels


def _train_synthetic_bundle(dim: int = _DIM) -> tuple[HeadBundle, HeadMetadata]:
    # category は high/low → "vuln" (240 件)、medium → "malware" (120 件) に合流させ、
    # 両カテゴリとも最小学習件数 (100) 以上にして category モデルも学習対象にする。
    x_train, y_imp_train, y_cat_train = _make_synthetic_dataset(n_per_class=120, dim=dim)
    x_eval, y_imp_eval, y_cat_eval = _make_synthetic_dataset(n_per_class=30, dim=dim)
    return train_head(
        x_train,
        y_imp_train,
        y_cat_train,
        x_eval,
        y_imp_eval,
        y_cat_eval,
        embedding_model=_EMBED_MODEL,
        dim=dim,
        trained_at="2026-08-22T00:00:00+00:00",
        rubric_version="v1",
    )


def test_train_head_learns_separable_importance_classes() -> None:
    # Arrange: 分離可能な 3 クラスの合成データ
    # Act
    bundle, metadata = _train_synthetic_bundle()
    # Assert: 分離可能なデータなので高い精度が出るはず
    assert metadata.importance_classes == ("high", "low", "medium")
    assert metadata.n_train == 360
    assert metadata.n_eval == 90
    assert metadata.metrics["importance"]["accuracy"] > 0.9
    assert isinstance(bundle, HeadBundle)


def test_predict_batch_probabilities_sum_to_one_per_row() -> None:
    # Arrange
    bundle, metadata = _train_synthetic_bundle()
    x_eval, _, _ = _make_synthetic_dataset(n_per_class=5)
    # Act
    predictions = predict_batch(bundle, x_eval)
    # Assert
    assert len(predictions) == x_eval.shape[0]
    for pred in predictions:
        total = sum(pred.importance_probs.values())
        assert total == pytest.approx(1.0, abs=1e-6)
        assert set(pred.importance_probs.keys()) == set(metadata.importance_classes)


def test_predict_batch_returns_category_when_category_model_trained() -> None:
    # Arrange: 合成データは category も 100 件超あるので category モデルが学習される
    bundle, metadata = _train_synthetic_bundle()
    x_eval, _, _ = _make_synthetic_dataset(n_per_class=3)
    # Act
    predictions = predict_batch(bundle, x_eval)
    # Assert
    assert metadata.category_classes  # 学習対象クラスが存在する
    for pred in predictions:
        assert pred.category in metadata.category_classes
        assert pred.category_prob is not None
        assert 0.0 <= pred.category_prob <= 1.0


def test_save_and_load_artifact_roundtrip_preserves_predictions(tmp_path: Path) -> None:
    # Arrange
    bundle, metadata = _train_synthetic_bundle()
    x_eval, _, _ = _make_synthetic_dataset(n_per_class=4)
    expected = predict_batch(bundle, x_eval)
    out_dir = tmp_path / "head_artifact"
    # Act
    save_artifact(out_dir, bundle, metadata)
    loaded_bundle, loaded_metadata = load_artifact(out_dir, _EMBED_MODEL, expected_dim=_DIM)
    actual = predict_batch(loaded_bundle, x_eval)
    # Assert
    assert loaded_metadata == metadata
    assert [p.importance for p in actual] == [p.importance for p in expected]
    assert [p.category for p in actual] == [p.category for p in expected]


def test_load_artifact_raises_on_embedding_model_mismatch(tmp_path: Path) -> None:
    # Arrange
    bundle, metadata = _train_synthetic_bundle()
    out_dir = tmp_path / "head_artifact"
    save_artifact(out_dir, bundle, metadata)
    # Act / Assert
    with pytest.raises(HeadArtifactMismatchError):
        load_artifact(out_dir, "different-embed-model")


def test_load_artifact_raises_on_dim_mismatch_when_expected_dim_given(tmp_path: Path) -> None:
    # Arrange
    bundle, metadata = _train_synthetic_bundle()
    out_dir = tmp_path / "head_artifact"
    save_artifact(out_dir, bundle, metadata)
    # Act / Assert
    with pytest.raises(HeadArtifactMismatchError):
        load_artifact(out_dir, _EMBED_MODEL, expected_dim=_DIM + 1)


def test_load_artifact_skips_dim_check_when_expected_dim_omitted(tmp_path: Path) -> None:
    # Arrange: head_shadow.py の呼出契約 (embedding_model のみ渡す) を再現
    bundle, metadata = _train_synthetic_bundle()
    out_dir = tmp_path / "head_artifact"
    save_artifact(out_dir, bundle, metadata)
    # Act: expected_dim を渡さない
    _loaded_bundle, loaded_metadata = load_artifact(out_dir, _EMBED_MODEL)
    # Assert: 例外にならず、dim はメタデータ通り読める
    assert loaded_metadata.dim == _DIM


def test_load_artifact_raises_when_artifact_missing(tmp_path: Path) -> None:
    # Arrange: 何も保存していない空ディレクトリ
    out_dir = tmp_path / "missing"
    # Act / Assert
    with pytest.raises(HeadArtifactMismatchError):
        load_artifact(out_dir, _EMBED_MODEL)


def test_category_classes_exclude_classes_below_minimum_train_count() -> None:
    # Arrange: catA/catB は 150 件ずつ (学習対象)、catC は 10 件のみ (対象外)
    rng = np.random.default_rng(seed=7)
    dim = 4
    n_a, n_b, n_c = 150, 150, 10
    x = rng.normal(size=(n_a + n_b + n_c, dim)).astype(np.float32)
    y_imp = ["high"] * n_a + ["medium"] * n_b + ["low"] * n_c
    y_cat = _repeat_labels([("catA", n_a), ("catB", n_b), ("catC", n_c)])
    x_eval = rng.normal(size=(20, dim)).astype(np.float32)
    y_imp_eval = ["high"] * 7 + ["medium"] * 7 + ["low"] * 6
    y_cat_eval = _repeat_labels([("catA", 7), ("catB", 7), ("catC", 6)])

    # Act
    _bundle, metadata = train_head(
        x,
        y_imp,
        y_cat,
        x_eval,
        y_imp_eval,
        y_cat_eval,
        embedding_model=_EMBED_MODEL,
        dim=dim,
        trained_at="2026-08-22T00:00:00+00:00",
    )

    # Assert: catC (10 件、100 件未満) は category_classes に入らない
    assert set(metadata.category_classes) == {"catA", "catB"}
    assert "catC" not in metadata.category_classes
    assert metadata.metrics["category"]["trained_classes"] == 2


def test_category_model_untrained_when_no_class_reaches_minimum() -> None:
    # Arrange: すべてのクラスが 100 件未満
    rng = np.random.default_rng(seed=3)
    dim = 4
    x = rng.normal(size=(30, dim)).astype(np.float32)
    y_imp = ["high"] * 10 + ["medium"] * 10 + ["low"] * 10
    y_cat = _repeat_labels([("catA", 15), ("catB", 15)])
    x_eval = rng.normal(size=(10, dim)).astype(np.float32)
    y_imp_eval = ["high"] * 4 + ["medium"] * 3 + ["low"] * 3
    y_cat_eval = _repeat_labels([("catA", 5), ("catB", 5)])

    # Act
    bundle, metadata = train_head(
        x,
        y_imp,
        y_cat,
        x_eval,
        y_imp_eval,
        y_cat_eval,
        embedding_model=_EMBED_MODEL,
        dim=dim,
        trained_at="2026-08-22T00:00:00+00:00",
    )

    # Assert
    assert bundle.category_model is None
    assert metadata.category_classes == ()
    assert metadata.metrics["category"]["coverage"] == 0.0


def test_ece_is_near_zero_when_confidence_matches_accuracy_per_bin() -> None:
    # Arrange: confidence=0.7 の 10 件中 7 件正解 → その bin の acc=conf=0.7
    confidences = [0.7] * 10
    correct = [True] * 7 + [False] * 3
    # Act
    result = ece(confidences, correct, n_bins=10)
    # Assert
    assert result == pytest.approx(0.0, abs=1e-9)


def test_ece_is_half_when_all_confidence_is_one_and_half_are_wrong() -> None:
    # Arrange: 確信度 1.0 だが半分誤り → |acc(0.5) - conf(1.0)| = 0.5
    confidences = [1.0] * 10
    correct = [True] * 5 + [False] * 5
    # Act
    result = ece(confidences, correct, n_bins=10)
    # Assert
    assert result == pytest.approx(0.5, abs=1e-9)


def test_ece_returns_zero_for_empty_input() -> None:
    # Arrange / Act
    result = ece([], [])
    # Assert
    assert result == 0.0
