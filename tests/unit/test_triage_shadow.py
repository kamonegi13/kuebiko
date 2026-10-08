"""src.tools.triage_shadow のテスト (2026-10-08、M4)。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.cti.relevance_ml import JapanInvolvementJudgement, load_relevance_jp_model
from src.tools.article_model import Article
from src.tools.article_triage import TriageDecision
from src.tools.triage_shadow import run_triage_shadow, shadow_sample_size


def _article(i: int, title: str = "記事") -> Article:
    return Article(
        id=f"rss:{i}",
        title=title,
        url=f"https://example.com/{i}",
        summary_html="<p>概要</p>",
        author=None,
        published=datetime.now(UTC),
        feed_title="Feed",
        feed_url="https://example.com/feed",
    )


def test_shadow_sample_size_defaults_to_20(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRIAGE_SHADOW_PER_RUN", raising=False)
    assert shadow_sample_size() == 20


def test_shadow_sample_size_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "0")
    assert shadow_sample_size() == 0


def test_shadow_sample_size_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "not-a-number")
    assert shadow_sample_size() == 20


@pytest.mark.asyncio
async def test_disabled_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "0")
    llm = AsyncMock()
    rows = await run_triage_shadow(
        [(_article(1), "low", False)], llm=llm, keep_importance={"high", "medium"}
    )
    assert rows == []
    llm.generate_structured.assert_not_called()


@pytest.mark.asyncio
async def test_builds_row_with_new_kept_from_flat_importance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "5")
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="medium", reason="平たい判定で medium")
    )
    rows = await run_triage_shadow(
        [(_article(1, "無関係な記事"), "low", False)],
        llm=llm,
        keep_importance={"high", "medium"},
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.article_id == "rss:1"
    assert row.current_importance == "low"
    assert row.current_kept is False
    assert row.flat_importance == "medium"
    assert row.new_kept is True  # flat importance が keep_importance 内


@pytest.mark.asyncio
async def test_new_kept_true_when_hint_fires_even_if_flat_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "5")
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="low", reason="平たい判定は low")
    )
    rows = await run_triage_shadow(
        [(_article(1, "日本の重要インフラ企業への攻撃"), "low", False)],
        llm=llm,
        keep_importance={"high", "medium"},
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.flat_importance == "low"
    assert row.hint_fired is True
    assert "jp" in row.hint_reasons
    assert row.new_kept is True


@pytest.mark.asyncio
async def test_sample_capped_at_per_run_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "2")
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(return_value=TriageDecision(importance="low", reason=""))
    decisions = [(_article(i), "low", False) for i in range(5)]
    rows = await run_triage_shadow(decisions, llm=llm, keep_importance={"high", "medium"})
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_single_article_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "5")
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(side_effect=RuntimeError("llm down"))
    rows = await run_triage_shadow(
        [(_article(1), "low", False)], llm=llm, keep_importance={"high", "medium"}
    )
    # triage_flat 自体が fail-open で medium を返すため、ここでは例外は飲まれて行は作られる
    assert len(rows) == 1
    assert rows[0].flat_importance == "medium"


# ---------------------------------------------------------------------------
# 日本関連性 ML カスケード (M4、2026-10-08)
# ---------------------------------------------------------------------------


def _write_jp_model(tmp_path: Path) -> Path:
    gaz_path = tmp_path / "gaz.json"
    gaz_path.write_text(json.dumps({"entries": []}), encoding="utf-8")
    model_path = tmp_path / "model.json"
    model_path.write_text(
        json.dumps(
            {
                "label": "y_jp",
                "embedding_model": "fake-embed",
                "coef": [1.0, 0.0, 0.0],
                "intercept": 0.0,
                "scaler_mean": [0.0],
                "scaler_scale": [1.0],
                "feed_prior_table": {"__global__": 0.0},
                "gazetteer_path": str(gaz_path),
                "threshold_recall98": 0.9,
                "threshold_recall99": 0.1,
                "threshold_precision95": 0.9,
            }
        ),
        encoding="utf-8",
    )
    return model_path


class _FakeEmbedder:
    def __init__(self, model: str, value: float) -> None:
        self.model = model
        self._value = value

    async def embed(self, text: str, *, kind: str = "document") -> object:
        class _Resp:
            vector = (self._value,)

        return _Resp()


def _sigmoid_inv_above(threshold: float) -> float:
    """threshold より確率が高くなる embedding 値 (z=x, sigmoid(x)>threshold)。"""
    import math

    return math.log(threshold / (1 - threshold)) + 1.0


@pytest.mark.asyncio
async def test_no_embedder_leaves_jp_fields_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "5")
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(return_value=TriageDecision(importance="low", reason=""))
    rows = await run_triage_shadow(
        [(_article(1, "無関係"), "low", False)], llm=llm, keep_importance={"high", "medium"}
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.jp_prob is None
    assert row.jp_ml_fired is None
    assert row.jp_cascade is None
    # new_kept_v2 は flat/hint だけで計算される (jp 系なし)
    assert row.new_kept_v2 is False


@pytest.mark.asyncio
async def test_cascade_fires_above_band_without_llm_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "5")
    load_relevance_jp_model.cache_clear()
    model_path = _write_jp_model(tmp_path)
    monkeypatch.setattr(
        "src.tools.triage_shadow.load_relevance_jp_model",
        lambda: load_relevance_jp_model(model_path),
    )
    triage_llm = AsyncMock()
    triage_llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="low", reason="")
    )
    cascade_llm = AsyncMock()
    cascade_llm.generate_structured = AsyncMock(
        side_effect=AssertionError("帯外では LLM を呼ばない")
    )
    embedder = _FakeEmbedder(model="fake-embed", value=_sigmoid_inv_above(0.95))
    rows = await run_triage_shadow(
        [(_article(1, "無関係"), "low", False)],
        llm=triage_llm,
        keep_importance={"high", "medium"},
        relevance_embedder=embedder,  # type: ignore[arg-type]
        relevance_cascade_llm=cascade_llm,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.jp_prob is not None and row.jp_prob > 0.9
    assert row.jp_ml_fired is True
    assert row.jp_cascade is None  # 帯外なので LLM 未呼出
    assert row.new_kept_v2 is True
    cascade_llm.generate_structured.assert_not_called()


@pytest.mark.asyncio
async def test_cascade_asks_llm_inside_band(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "5")
    load_relevance_jp_model.cache_clear()
    model_path = _write_jp_model(tmp_path)
    monkeypatch.setattr(
        "src.tools.triage_shadow.load_relevance_jp_model",
        lambda: load_relevance_jp_model(model_path),
    )
    triage_llm = AsyncMock()
    triage_llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="low", reason="")
    )
    cascade_llm = AsyncMock()
    cascade_llm.generate_structured = AsyncMock(
        return_value=JapanInvolvementJudgement(japan_involved=True, reason="日本企業が標的")
    )
    # z=0 → sigmoid=0.5、帯 (0.1, 0.9) 内
    embedder = _FakeEmbedder(model="fake-embed", value=0.0)
    rows = await run_triage_shadow(
        [(_article(1, "無関係"), "low", False)],
        llm=triage_llm,
        keep_importance={"high", "medium"},
        relevance_embedder=embedder,  # type: ignore[arg-type]
        relevance_cascade_llm=cascade_llm,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.jp_prob == pytest.approx(0.5)
    assert row.jp_ml_fired is True  # 0.5 >= threshold_recall99(0.1)
    assert row.jp_cascade is True
    assert row.new_kept_v2 is True
    cascade_llm.generate_structured.assert_called_once()


@pytest.mark.asyncio
async def test_cascade_not_fired_below_band_without_llm_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "5")
    load_relevance_jp_model.cache_clear()
    model_path = _write_jp_model(tmp_path)
    monkeypatch.setattr(
        "src.tools.triage_shadow.load_relevance_jp_model",
        lambda: load_relevance_jp_model(model_path),
    )
    triage_llm = AsyncMock()
    triage_llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="low", reason="")
    )
    cascade_llm = AsyncMock()
    cascade_llm.generate_structured = AsyncMock(
        side_effect=AssertionError("帯外では LLM を呼ばない")
    )
    embedder = _FakeEmbedder(model="fake-embed", value=-10.0)  # sigmoid(-10) ≈ 0
    rows = await run_triage_shadow(
        [(_article(1, "無関係"), "low", False)],
        llm=triage_llm,
        keep_importance={"high", "medium"},
        relevance_embedder=embedder,  # type: ignore[arg-type]
        relevance_cascade_llm=cascade_llm,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.jp_ml_fired is False
    assert row.jp_cascade is None
    assert row.new_kept_v2 is False
    cascade_llm.generate_structured.assert_not_called()
