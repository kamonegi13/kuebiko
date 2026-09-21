"""深掘りの ML 前段 (2026-09-21)。

⭐ **置き換えではなく絞り込み**。同じモデルでも役割で成否が変わる:
上位 20 をそのまま採ると LLM の選抜との一致は 37%、上位 90 まで通せば 90%。
比較対象の LLM 自身が自己一致 70% なので、top-90 の損失はその揺らぎより小さい。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.digest.deep_dive_features import DD_FEATURE_NAMES
from src.digest.deep_dive_ml import (
    PREFILTER_TOP_K_DEFAULT,
    load_model,
    prefilter_select,
    prefilter_top_k,
)


class TestPrefilterSelect:
    def test_takes_the_top_k_by_predicted_score(self) -> None:
        scores = {"a": 0.1, "b": 0.9, "c": 0.5, "d": 0.7}

        assert prefilter_select(scores, top_k=2) == ["b", "d"]

    def test_zero_means_do_not_narrow(self) -> None:
        """0 は「絞らない」の合図。空を返して呼び手が素通しする。"""
        assert prefilter_select({"a": 1.0}, top_k=0) == []


class TestTopK:
    def test_default_is_ninety(self) -> None:
        """実測で top-90 が LLM の上位 20 の 90% を覆う。"""
        assert PREFILTER_TOP_K_DEFAULT == 90

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPDIVE_ML_PREFILTER", "40")
        assert prefilter_top_k() == 40

    def test_garbage_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPDIVE_ML_PREFILTER", "zzz")
        assert prefilter_top_k() == PREFILTER_TOP_K_DEFAULT


class TestModelGate:
    """⚠ 列がずれていたら黙って別の特徴で採点しない。"""

    def test_column_mismatch_yields_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.digest.deep_dive_ml as mod

        monkeypatch.setattr(mod, "_loaded", False)
        monkeypatch.setattr(mod, "_model", None)
        p = tmp_path / "m.json"
        p.write_text(json.dumps({"feature_names": ["a", "b"]}), encoding="utf-8")

        assert load_model(p) is None

    def test_missing_file_yields_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.digest.deep_dive_ml as mod

        monkeypatch.setattr(mod, "_loaded", False)
        monkeypatch.setattr(mod, "_model", None)

        assert load_model(tmp_path / "absent.json") is None

    def test_the_shipped_model_matches_the_feature_contract(self) -> None:
        import src.digest.deep_dive_ml as mod

        mod._loaded = False
        mod._model = None
        m = load_model()

        assert m is not None, "同梱モデルが列ずれしている"
        assert list(m["feature_names"]) == list(DD_FEATURE_NAMES)
