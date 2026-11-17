"""地政学の救済: v2 と学習版の和集合 (GEO_RESCUE_LEARNED=union)。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import src.pipeline.geo_rescue_learned as gl
import src.pipeline.geo_rescue_v2 as gv2
from src.tools.article_model import Article
from src.tools.article_triage import TriageDecision


def _article() -> Article:
    return Article(
        id="a1",
        url="https://example.com/a1",
        title="Russia drills",
        summary_html="",
        published=datetime(2026, 11, 17, tzinfo=UTC),
        feed_title="Feed",
        feed_url="https://example.com/feed",
    )


def _outcome(rescued: bool) -> gv2.V2Outcome:
    return gv2.V2Outcome(
        flat_rescued=False,
        pir_rescued=False,
        rescued=rescued,
        reason_text="r",
        cue_kinds=("nation",),
    )


async def _run(
    monkeypatch: pytest.MonkeyPatch, *, v2: gv2.V2Outcome, learned: Any
) -> gv2.V2Outcome:
    async def fake_decide(*_a: Any, **_k: Any) -> gv2.V2Outcome:
        return v2

    calls: list[int] = []

    async def fake_learned(*_a: Any, **_k: Any) -> float | None:
        calls.append(1)
        if isinstance(learned, Exception):
            raise learned
        return float(learned) if learned is not None else None

    monkeypatch.setattr(gv2, "_decide", fake_decide)
    monkeypatch.setattr(gv2, "_learned_probability", fake_learned)
    out = await gv2.decide_v2(
        _article(),
        TriageDecision(importance="low", reason="r"),
        preview="text",
        prompt_mode="flat",
        rescue_llm=object(),  # type: ignore[arg-type]
        think=False,
        config=object(),  # type: ignore[arg-type]
        keep={"medium", "high"},
    )
    _run.calls = len(calls)  # type: ignore[attr-defined]
    return out


def test_learned_mode_accepts_union(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_LEARNED", "union")
    assert gl.learned_mode() == "union"
    monkeypatch.setenv("GEO_RESCUE_LEARNED", "1")
    assert gl.learned_mode() == "0"


@pytest.mark.asyncio
async def test_union_rescues_when_learned_says_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_LEARNED", "union")
    monkeypatch.setattr(gv2, "_learned_threshold", lambda: 0.4)
    out = await _run(monkeypatch, v2=_outcome(False), learned=0.9)
    assert out.rescued is True
    assert out.learned_p == 0.9


@pytest.mark.asyncio
async def test_union_keeps_v2_no_when_learned_says_no(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_LEARNED", "union")
    monkeypatch.setattr(gv2, "_learned_threshold", lambda: 0.4)
    out = await _run(monkeypatch, v2=_outcome(False), learned=0.1)
    assert out.rescued is False
    assert out.learned_p == 0.1


@pytest.mark.asyncio
async def test_learned_failure_keeps_v2_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_LEARNED", "union")
    monkeypatch.setattr(gv2, "_learned_threshold", lambda: 0.4)
    out = await _run(monkeypatch, v2=_outcome(False), learned=RuntimeError("x"))
    assert out.rescued is False
    assert out.fallback is None  # v2 の結果は倒さない


@pytest.mark.asyncio
async def test_v2_rescued_skips_learned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_LEARNED", "union")
    out = await _run(monkeypatch, v2=_outcome(True), learned=0.0)
    assert out.rescued is True
    assert _run.calls == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_shadow_mode_does_not_touch_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEO_RESCUE_LEARNED", "shadow")
    out = await _run(monkeypatch, v2=_outcome(False), learned=0.99)
    assert out.rescued is False
    assert _run.calls == 0  # type: ignore[attr-defined]
