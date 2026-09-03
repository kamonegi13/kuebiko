"""本番 per-article triage が ``Step.TRIAGE`` で解決されることを固定する (2026-09-04)。

以前は要約用 client (``Step.ARTICLE_SUMMARY``) を triage に流用していた。両方とも fast
ティアなので**挙動の差は出ず、壊れていることに気付けない**が、次の 2 つが静かに破れる:

- ティアを分けても効かない — ``STEP_REGISTRY[Step.TRIAGE]`` が本番経路を支配しない
- 週次ドリフト検知 (``src/eval/triage_drift.py`` は ``Step.TRIAGE`` で組む) が
  **本番と別の client を測る**

どちらも「同じモデルに解決している間は無症状」なので、テストで配線そのものを固定する。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from src.config_loader import PipelineConfig, ProcessorConfig, SourceConfig
from src.pipeline import dispatch
from src.tools.model_tiers import Step


class _StubLLM:
    """どの Step から組まれたかだけを持つ stub。"""

    def __init__(self, step: Step) -> None:
        self.step = step


class _NullAsyncCtx:
    async def __aenter__(self) -> _NullAsyncCtx:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _StubDedupRepo:
    def filter_seen_and_touch(self, *_: object, **__: object) -> list[str]:
        return []


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """dispatch の I/O をすべて塞ぎ、LLM 構築と run_pipeline の引数だけを捕まえる。"""
    seen: dict[str, Any] = {"steps": [], "kwargs": {}}

    def fake_build_llm_for(step: Step, config: object) -> _StubLLM:
        seen["steps"].append(step)
        return _StubLLM(step)

    async def fake_run_pipeline(**kwargs: Any) -> str:
        seen["kwargs"] = kwargs
        return "done"

    pipeline = PipelineConfig(
        name="daily-briefing",
        source=SourceConfig(type="rss", max_articles=1),
        processor=ProcessorConfig(),
    )

    monkeypatch.setattr(dispatch, "build_llm_for", fake_build_llm_for)
    monkeypatch.setattr(dispatch, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(dispatch, "load_app_config", lambda *a, **k: object())
    monkeypatch.setattr(dispatch, "load_pipelines", lambda *a, **k: [pipeline])
    monkeypatch.setattr(dispatch, "_find_pipeline", lambda *a, **k: pipeline)
    monkeypatch.setattr(dispatch, "_load_template", lambda *a, **k: object())
    monkeypatch.setattr(dispatch, "load_channel_routing", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "load_llm_enrichment", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "load_stix_attach_policy", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "RunHistoryRepository", lambda *a, **k: _StubDedupRepo())
    monkeypatch.setattr(dispatch, "_try_build_embedder", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "ContentExtractor", lambda *a, **k: _NullAsyncCtx())
    monkeypatch.setattr(dispatch, "build_source", lambda *a, **k: object())
    monkeypatch.setattr(dispatch, "_build_publishers", lambda *a, **k: {})
    yield seen


@pytest.mark.asyncio
async def test_triage_client_is_built_from_triage_step(captured: dict[str, Any]) -> None:
    await dispatch.run_default(dry_run=True)

    assert Step.TRIAGE in captured["steps"], "triage 用 client が Step.TRIAGE で組まれていない"
    assert Step.ARTICLE_SUMMARY in captured["steps"]

    triage_llm = captured["kwargs"]["triage_llm"]
    summary_llm = captured["kwargs"]["llm"]
    assert isinstance(triage_llm, _StubLLM)
    assert isinstance(summary_llm, _StubLLM)
    assert triage_llm.step is Step.TRIAGE
    assert summary_llm.step is Step.ARTICLE_SUMMARY
    # 要約用を使い回していないこと (以前の欠陥はここが同一インスタンスだった)
    assert triage_llm is not summary_llm
