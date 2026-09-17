"""detect ML の推論と shadow 選抜 (2026-09-17)。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from src.synthesis.grounded.detect_features import FEATURE_NAMES
from src.synthesis.grounded.detect_ml import (
    DetectModel,
    load_detect_model,
    shadow_enabled,
    shadow_select,
)


def _model(n: int = 3) -> DetectModel:
    return DetectModel(
        feature_names=tuple(f"f{i}" for i in range(n)),
        mean=(0.0,) * n,
        scale=(1.0, 2.0, 0.0),
        coef=(1.0, -1.0, 5.0),
        intercept=0.5,
        threshold=0.5,
    )


def test_probability_is_standardized_logistic_and_ignores_zero_scale() -> None:
    m = _model()
    # z = 0.5 + 1.0*(2/1) + (-1.0)*(4/2) + 0 (scale 0 の列は寄与しない) = 0.5
    assert m.probability([2.0, 4.0, 100.0]) == pytest.approx(1 / (1 + math.exp(-0.5)))


def test_probability_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match="特徴量の数"):
        _model().probability([1.0])


def test_load_returns_none_when_feature_names_drift(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    p.write_text(
        json.dumps(
            {
                "feature_names": ["x"],
                "mean": [0],
                "scale": [1],
                "coef": [1],
                "intercept": 0,
                "threshold": 0.5,
            }
        )
    )
    assert load_detect_model(p) is None
    assert load_detect_model(tmp_path / "missing.json") is None


def test_load_accepts_current_feature_names(tmp_path: Path) -> None:
    n = len(FEATURE_NAMES)
    p = tmp_path / "m.json"
    p.write_text(
        json.dumps(
            {
                "feature_names": list(FEATURE_NAMES),
                "mean": [0] * n,
                "scale": [1] * n,
                "coef": [0] * n,
                "intercept": 0,
                "threshold": 0.4,
            }
        )
    )
    m = load_detect_model(p)
    assert m is not None and m.threshold == 0.4
    assert m.probability([0.0] * n) == pytest.approx(0.5)


def test_shadow_select_orders_by_probability_and_caps() -> None:
    scores = {"a": 0.9, "b": 0.2, "c": 0.7, "d": 0.7}
    assert shadow_select(scores, threshold=0.5, top_k=2) == [("a", 0.9), ("c", 0.7)]
    assert shadow_select(scores, threshold=0.95) == []


def test_shadow_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DETECT_ML_SHADOW", raising=False)
    assert shadow_enabled() is True
    monkeypatch.setenv("DETECT_ML_SHADOW", "0")
    assert shadow_enabled() is False


class _FakeRepo:
    def __init__(self, cached: dict[str, str]) -> None:
        self.cached = dict(cached)
        self.written: list[tuple[str, str, str]] = []

    def get_article_kinds(self, ids: list[str]) -> dict[str, str]:
        return {a: k for a, k in self.cached.items() if a in ids}

    def set_article_kind(self, aid: str, kind: str, model: str) -> None:
        self.written.append((aid, kind, model))


def test_ensure_kinds_classifies_only_missing_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from src.synthesis.grounded.detect_ml import ensure_kinds

    repo = _FakeRepo({"a": "breach"})
    calls: list[str] = []

    async def classify(title: str, summary: str) -> str:
        calls.append(title)
        return "exploitation"

    kinds = asyncio.run(
        ensure_kinds(
            repo,
            [("a", "ta", "sa"), ("b", "tb", "sb"), ("c", "tc", "sc")],
            classify,
            model_label="m",
            limit=1,
        )
    )

    assert kinds == {"a": "breach", "b": "exploitation"}  # c は上限超過で未分類 (= other 扱い)
    assert calls == ["tb"]
    assert repo.written == [("b", "exploitation", "m")]


def test_prefilter_select_orders_by_probability_and_zero_means_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.synthesis.grounded.detect_ml import prefilter_select, prefilter_top_k

    scores = {"a": 0.1, "b": 0.9, "c": 0.5}
    assert prefilter_select(scores, top_k=2) == ["b", "c"]
    assert prefilter_select(scores, top_k=0) == ["b", "c", "a"]
    monkeypatch.delenv("DETECT_ML_PREFILTER", raising=False)
    assert prefilter_top_k() == 15
    monkeypatch.setenv("DETECT_ML_PREFILTER", "0")
    assert prefilter_top_k() == 0
    monkeypatch.setenv("DETECT_ML_PREFILTER", "abc")
    assert prefilter_top_k() == 15


def test_rollup_titles_are_detected() -> None:
    from src.synthesis.grounded.detect_ml import is_rollup_title

    assert is_rollup_title(
        "マイクロソフトが 8 月のセキュリティ情報公開、悪用の事実を確認済みの脆弱性が 1 件"
    )
    assert is_rollup_title("Microsoftが8月の月例更新を公開 優先すべきは「緊急」ではない")
    assert is_rollup_title(
        "CISA、実悪用が確認された TrueConf-Server の脆弱性 2 件を KEV カタログに追加"
    )
    assert is_rollup_title("Shadowserver、悪用が確認された脆弱性の注意喚起を発信")
    assert not is_rollup_title(
        "Head Mare APT、TrueConf Server の RCE 脆弱性を悪用し PhantomCore を配布"
    )
    assert not is_rollup_title("A 社への不正アクセス、調査継続と新事実の判明")


def test_compose_candidates_adds_high_floor_and_drops_excluded() -> None:
    from src.synthesis.grounded.detect_ml import compose_llm_candidates

    scores = {"a": 0.9, "b": 0.8, "c": 0.7, "roll": 0.99, "hi": 0.1}
    got = compose_llm_candidates(scores, top_k=2, high_ids={"hi", "roll", "a"}, excluded={"roll"})
    assert got == ["a", "b", "hi"]  # ML 上位 2 (roll は除外) + high の下限保証 (a は既出)
    # モデル無し (top_k=0 で全件) でも除外だけは効く
    assert compose_llm_candidates(
        dict.fromkeys(["x", "roll"], 0.0), top_k=0, high_ids=set(), excluded={"roll"}
    ) == ["x"]
