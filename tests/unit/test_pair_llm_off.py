"""旗 EVENTNEWS_PAIR_LLM — pair 判定の LLM を外す道。既定 1 は従来どおり。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import numpy as np
import pytest

from src.config_loader import AppConfig
from src.eventnews import pair_model, pair_shadow
from src.eventnews.models import MemberArticle
from src.tools.llm_client import LLMClient

_ENTS = frozenset({("cve", "CVE-2026-1"), ("victim_org", "alpha")})
_VEC = np.asarray([1.0, 0.0], dtype=np.float32)


def _member(aid: str) -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=f"見出し {aid}",
        url=f"https://example.test/{aid}",
        feed_title="媒体",
        feed_url="https://example.test/feed",
        host="example.test",
        importance="high",
        category="breach",
        status="posted",
        anchor_ts=datetime(2026, 8, 31, tzinfo=UTC),
        summary="要約",
        body="本文",
        entities=_ENTS,
    )


async def _embed(arts):  # type: ignore[no-untyped-def]
    return {a.article_id: _VEC for a in arts}


class _CountingLlm:
    model = "test"
    calls = 0

    async def generate_structured(self, *a, **k):  # type: ignore[no-untyped-def]
        type(self).calls += 1
        from types import SimpleNamespace

        return SimpleNamespace(same_event=True)


def test_flag_defaults_to_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVENTNEWS_PAIR_LLM", raising=False)
    assert pair_model.is_llm_enabled()
    monkeypatch.setenv("EVENTNEWS_PAIR_LLM", "1")
    assert pair_model.is_llm_enabled()
    monkeypatch.setenv("EVENTNEWS_PAIR_LLM", "0")
    assert not pair_model.is_llm_enabled()


@pytest.mark.asyncio
async def test_llm_none_makes_no_call_and_uses_llm_off_threshold() -> None:
    model = pair_model.load_model()
    assert model is not None
    pairs = [(_member("a"), _member("b"))]
    verdicts = await pair_shadow.judge_pairs(
        pairs, {"a": _VEC, "b": _VEC}, llm=None, embed_summary=_embed
    )
    v = verdicts[0]
    assert v.llm_same is None
    assert v.ml_proba is not None
    full = v.features + [0.0, 0.0]  # llm_same / llm_known とも欠け
    assert v.ml_proba == model.probability(full)
    assert v.ml_joined == (v.ml_proba >= model.threshold_llm_off)


@pytest.mark.asyncio
async def test_llm_given_still_calls_llm_and_uses_llm_on_threshold() -> None:
    """旗 1 (既定) の経路は従来どおり。"""
    _CountingLlm.calls = 0
    verdicts = await pair_shadow.judge_pairs(
        [(_member("a"), _member("b"))],
        {"a": _VEC, "b": _VEC},
        llm=cast(LLMClient, _CountingLlm()),
        embed_summary=_embed,
    )
    assert _CountingLlm.calls == 1
    assert verdicts[0].llm_same is not None


def test_pair_llm_builder_is_not_called_when_flag_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.ui.services import eventnews_hourly_job as job

    built: list[object] = []
    sentinel = object()

    def fake_build(step: object, config: object) -> object:
        built.append(step)
        return sentinel

    monkeypatch.setattr(job, "build_llm_for", fake_build)
    cfg = cast(AppConfig, object())
    monkeypatch.setenv("EVENTNEWS_PAIR_LLM", "0")
    assert job._pair_llm(cfg) is None
    assert built == []
    monkeypatch.setenv("EVENTNEWS_PAIR_LLM", "1")
    assert job._pair_llm(cfg) is sentinel
    assert len(built) == 1


def test_model_check_passes_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """trained_llm_models が空 (現行モデル) なら LLM なしでも警告しない (旗 0 は照合を飛ばす)。"""
    model = pair_model.load_model()
    assert model is not None
    assert model.trained_llm_models == ()
    assert not pair_model.pair_llm_mismatch(model, "anything")
    monkeypatch.setenv("EVENTNEWS_PAIR_LLM", "0")
    pair_model.warn_on_pair_llm_mismatch(model)  # 例外なし
