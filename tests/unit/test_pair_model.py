"""学習済みモデルの推論が、学習時 (sklearn) と同じ結果を出すこと。

⚠ **これが合わなければ、実験で測った 90% は本番では出ない。** 推論は numpy で
書き直しているので (sklearn を本番に持ち込まないため)、一致を契約として固定する。

⭐ 確率には僅かな差が出る (実測 365 件中 8 件・最大 0.0025)。木の分岐の浮動小数の
境界で、400 本中 1 本が別の葉に落ちるため。**判定 (閾値との比較) は 365/365 で
一致**するので、許容差を置いて確率を照合し、判定は完全一致を求める。
"""

from __future__ import annotations

import json

from src.eventnews.pair_model import DEFAULT_MODEL_PATH, load_model

#: 確率の許容差。実測の最大差 0.0025 に余裕を持たせる。
_TOLERANCE = 0.01


def test_model_loads() -> None:
    model = load_model()
    assert model is not None, "config/models/pair_model.json が読めない"
    assert len(model.feature_names) == 21
    assert model.feature_names[-2:] == ("llm_same", "llm_known")


def test_inference_reproduces_training_probabilities() -> None:
    """モデルに埋め込んだ参照点で、学習時の確率を再現する。"""
    model = load_model()
    assert model is not None
    raw = json.loads(DEFAULT_MODEL_PATH.read_text(encoding="utf-8"))
    ref = raw["reference_points"]
    for features, expected in zip(ref["X"], ref["proba"], strict=True):
        got = model.probability(features)
        assert abs(got - expected) < _TOLERANCE, f"{got} != {expected}"


def test_decisions_match_at_both_thresholds() -> None:
    """⭐ 確率の僅差が判定を変えないこと (実測 365/365 一致)。"""
    model = load_model()
    assert model is not None
    raw = json.loads(DEFAULT_MODEL_PATH.read_text(encoding="utf-8"))
    ref = raw["reference_points"]
    for features, expected in zip(ref["X"], ref["proba"], strict=True):
        for available, threshold in (
            (True, model.threshold_llm_on),
            (False, model.threshold_llm_off),
        ):
            assert model.joins(features, llm_available=available) == (expected >= threshold)


def test_missing_model_degrades_to_none() -> None:
    """モデルが無い / 壊れていても群化を止めない (呼び手が決定論へ退避)。"""
    assert load_model("/nonexistent/pair_model.json") is None


def test_feature_count_mismatch_is_rejected() -> None:
    """列数が合わない = 特徴量の定義が変わったのにモデルが古い。黙って通さない。"""
    model = load_model()
    assert model is not None
    try:
        model.probability([0.0] * 5)
    except ValueError as e:
        assert "特徴量の数" in str(e)
    else:
        raise AssertionError("列数の不一致を弾いていない")
