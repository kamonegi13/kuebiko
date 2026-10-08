"""src.cti.relevance_ml のテスト (M4、2026-10-08)。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.cti.relevance_ml import (
    JapanInvolvementJudgement,
    RelevanceJpModel,
    cascade_japan_involved,
    in_uncertain_band,
    jp_probability,
    load_relevance_jp_model,
    ml_fired,
)


def _write_model(tmp_path: Path, *, gazetteer_entries: list[str] | None = None) -> Path:
    gaz_path = tmp_path / "gaz.json"
    gaz_path.write_text(
        json.dumps({"entries": [{"name_norm": n} for n in (gazetteer_entries or [])]}),
        encoding="utf-8",
    )
    model_path = tmp_path / "model.json"
    # 2 次元 embedding の最小構成: coef = [emb_w0, emb_w1, feed_prior_w, gazetteer_w]
    model_path.write_text(
        json.dumps(
            {
                "label": "y_jp",
                "embedding_model": "snowflake-arctic-embed2",
                "coef": [1.0, 1.0, 2.0, 3.0],
                "intercept": -5.0,
                "scaler_mean": [0.0, 0.0],
                "scaler_scale": [1.0, 1.0],
                "feed_prior_table": {"feedA": 0.5, "__global__": 0.1},
                "gazetteer_path": str(gaz_path),
                "threshold_recall98": 0.5,
                "threshold_recall99": 0.1,
                "threshold_precision95": 0.9,
            }
        ),
        encoding="utf-8",
    )
    return model_path


class _FakeEmbedder:
    """EmbeddingClient 最小実装 (model 属性つき)。"""

    def __init__(self, model: str, vector: tuple[float, ...]) -> None:
        self.model = model
        self._vector = vector

    async def embed(self, text: str, *, kind: str = "document") -> object:
        class _Resp:
            vector = self._vector

        return _Resp()


def test_load_relevance_jp_model_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_relevance_jp_model(tmp_path / "nope.json") is None


def test_load_relevance_jp_model_reads_recipe(tmp_path: Path) -> None:
    path = _write_model(tmp_path)
    model = load_relevance_jp_model(path)
    assert model is not None
    assert model.embedding_model == "snowflake-arctic-embed2"
    assert model.emb_dim == 2


def test_probability_matches_manual_sigmoid(tmp_path: Path) -> None:
    path = _write_model(tmp_path)
    model = load_relevance_jp_model(path)
    assert model is not None
    prob = model.probability(emb=[1.0, 1.0], feed="feedA", gazetteer_hit=True)
    # z = -5 + 1*1 + 1*1 + 2*0.5 + 3*1 = -5+1+1+1+3 = 1.0
    expected = 1.0 / (1.0 + math.exp(-1.0))
    assert prob == pytest.approx(expected)


def test_probability_unknown_feed_uses_global_prior(tmp_path: Path) -> None:
    path = _write_model(tmp_path)
    model = load_relevance_jp_model(path)
    assert model is not None
    prob = model.probability(emb=[0.0, 0.0], feed="unknown-feed", gazetteer_hit=False)
    # z = -5 + 2*0.1 = -4.8
    expected = 1.0 / (1.0 + math.exp(4.8))
    assert prob == pytest.approx(expected)


def test_ml_fired_uses_recall99_threshold(tmp_path: Path) -> None:
    model = load_relevance_jp_model(_write_model(tmp_path))
    assert model is not None
    assert ml_fired(0.2, model) is True
    assert ml_fired(0.05, model) is False


def test_in_uncertain_band(tmp_path: Path) -> None:
    model = load_relevance_jp_model(_write_model(tmp_path))
    assert model is not None
    assert in_uncertain_band(0.05, model) is False  # 下限未満
    assert in_uncertain_band(0.5, model) is True  # 帯内
    assert in_uncertain_band(0.95, model) is False  # 上限以上


@pytest.mark.asyncio
async def test_jp_probability_returns_none_on_embedding_model_mismatch(tmp_path: Path) -> None:
    model = load_relevance_jp_model(_write_model(tmp_path))
    assert model is not None
    embedder = _FakeEmbedder(model="wrong-model", vector=(0.0, 0.0))
    result = await jp_probability(
        feed="feedA",
        title="t",
        preview="p",
        embedder=embedder,  # type: ignore[arg-type]
        model=model,
    )
    assert result is None


@pytest.mark.asyncio
async def test_jp_probability_returns_none_on_embed_failure(tmp_path: Path) -> None:
    model = load_relevance_jp_model(_write_model(tmp_path))
    assert model is not None

    class _FailingEmbedder:
        model = "snowflake-arctic-embed2"

        async def embed(self, text: str, *, kind: str = "document") -> object:
            raise RuntimeError("embed down")

    result = await jp_probability(
        feed="feedA",
        title="t",
        preview="p",
        embedder=_FailingEmbedder(),  # type: ignore[arg-type]
        model=model,
    )
    assert result is None


@pytest.mark.asyncio
async def test_jp_probability_computes_with_gazetteer_hit(tmp_path: Path) -> None:
    model = load_relevance_jp_model(_write_model(tmp_path, gazetteer_entries=["acme株式会社"]))
    assert model is not None
    embedder = _FakeEmbedder(model="snowflake-arctic-embed2", vector=(0.0, 0.0))
    prob = await jp_probability(
        feed="feedA",
        title="ACME株式会社が被害",
        preview="",
        embedder=embedder,  # type: ignore[arg-type]
        model=model,
    )
    assert prob is not None
    # gazetteer ヒット (全角/大小文字正規化で一致) + feed prior のみ寄与: z=-5+2*0.5+3=-1
    expected = 1.0 / (1.0 + math.exp(1.0))
    assert prob == pytest.approx(expected)


@pytest.mark.asyncio
async def test_cascade_japan_involved_returns_judgement() -> None:
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=JapanInvolvementJudgement(japan_involved=True, reason="日本企業が標的")
    )
    result = await cascade_japan_involved(title="t", preview="p", llm=llm)
    assert result is not None
    assert result.japan_involved is True


@pytest.mark.asyncio
async def test_cascade_japan_involved_returns_none_on_failure() -> None:
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(side_effect=RuntimeError("down"))
    result = await cascade_japan_involved(title="t", preview="p", llm=llm)
    assert result is None


def test_relevance_jp_model_probability_dimension_mismatch_raises(tmp_path: Path) -> None:
    model = load_relevance_jp_model(_write_model(tmp_path))
    assert model is not None
    with pytest.raises(ValueError):
        model.probability(emb=[0.0], feed="feedA", gazetteer_hit=False)


def test_relevance_jp_model_is_frozen(tmp_path: Path) -> None:
    model = load_relevance_jp_model(_write_model(tmp_path))
    assert model is not None
    assert isinstance(model, RelevanceJpModel)
    with pytest.raises(Exception):  # noqa: B017 — frozen dataclass は FrozenInstanceError
        model.intercept = 0.0  # type: ignore[misc]
