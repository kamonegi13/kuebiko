"""pair の学習スクリプトの純粋な部分 (sklearn 不要)。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from scripts.train_pair_model import (
    LLM_COLUMNS,
    N_REFERENCE_POINTS,
    assemble_model,
    kind_matrix,
    llm_columns,
    load_kinds,
    load_llm_scores,
    mask_llm,
)

PAIRS = [{"a": "x", "b": "y"}, {"a": "x", "b": "z"}, {"a": "y", "b": "z"}]


def test_mask_is_deterministic_and_does_not_mutate() -> None:
    scores, known = np.ones(1000), np.ones(1000)
    s1, k1 = mask_llm(scores, known)
    s2, k2 = mask_llm(scores, known)
    assert (s1 == s2).all() and (k1 == k2).all()
    assert scores.sum() == 1000 and known.sum() == 1000
    assert 0.25 < 1 - k1.mean() < 0.35  # 約 30% を伏せる
    assert ((k1 == 0) == (s1 == 0)).all()  # 伏せた所は (0, 0)


def test_llm_columns_default_keeps_base_scores() -> None:
    s, k = llm_columns(PAIRS, [1.0, 0.0, 1.0], None)
    assert s.tolist() == [1.0, 0.0, 1.0] and k.tolist() == [1.0, 1.0, 1.0]


def test_llm_columns_override_marks_failed_judgement_unknown() -> None:
    over: dict[tuple[str, str], bool | None] = {("x", "y"): True, ("x", "z"): None}
    s, k = llm_columns(PAIRS, [0.0, 1.0, 1.0], over)
    assert s.tolist() == [1.0, 0.0, 0.0]
    assert k.tolist() == [1.0, 0.0, 0.0]  # 未指定の対も不明 (旧 S の判定を混ぜない)


def test_kind_matrix_defaults_missing_to_other() -> None:
    m = kind_matrix(PAIRS, {"x": "advisory", "y": "breach"})
    assert m.shape == (3, 4)
    assert m[0].tolist() == [0.0, 1.0, 0.0, 0.0]  # advisory vs incident


def test_assemble_model_has_production_shape() -> None:
    xf = np.zeros((10, 3))
    out = assemble_model(
        names=["a", "b", "c"],
        n_samples=10,
        trees=[],
        xf=xf,
        proba=np.linspace(0, 1, 10),
        trained_at="2026-11-06",
        trained_llm_models=["m"],
    )
    assert list(out) == [
        "version",
        "trained_llm_models",
        "trained_at",
        "n_samples",
        "feature_names",
        "mask_rate",
        "threshold_llm_on",
        "threshold_llm_off",
        "reference_points",
        "trees",
    ]
    assert len(out["reference_points"]["X"]) == N_REFERENCE_POINTS
    assert LLM_COLUMNS == ("llm_same", "llm_known")


def test_loaders(tmp_path: Path) -> None:
    kj = tmp_path / "k.json"
    kj.write_text(json.dumps({"a": "breach"}), encoding="utf-8")
    kl = tmp_path / "k.jsonl"
    kl.write_text(json.dumps({"article_id": "a", "kind": "stats"}) + "\n", encoding="utf-8")
    assert load_kinds(kj) == {"a": "breach"}
    assert load_kinds(kl) == {"a": "stats"}
    sc = tmp_path / "s.jsonl"
    sc.write_text(
        json.dumps({"a": "x", "b": "y", "same": None})
        + "\n"
        + json.dumps({"a": "x", "b": "z", "same": True})
        + "\n",
        encoding="utf-8",
    )
    assert load_llm_scores(sc) == {("x", "y"): None, ("x", "z"): True}
