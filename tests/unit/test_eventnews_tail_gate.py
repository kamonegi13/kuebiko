"""事象ニュース draft の尾部全空関門 (2026-09-13)。

1. 判定器: 3 欄すべて空のときだけ発火 (個別の欄が空なのは教師でも普通)
2. 配線: 全空なら同一プロンプトで再サンプルし最良候補を採る / 上限後も空なら記録して通す
3. 旗 EVENTNEWS_TAIL_GATE=0 で無効
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from src.eventnews.generator import generate_draft
from src.eventnews.models import EventNewsDraft, FactItem, MemberArticle
from src.eventnews.tail_gate import MAX_RESAMPLES, tail_all_empty
from src.tools.llm_client import LLMClient


def _draft(disc: int = 0, cav: int = 0, unk: int = 0) -> EventNewsDraft:
    return EventNewsDraft(
        headline="h",
        bluf="b",
        facts=[FactItem(text="f", source_index=1)],
        discrepancies=[FactItem(text=f"d{i}", source_index=1) for i in range(disc)],
        caveats=[FactItem(text=f"c{i}", source_index=1) for i in range(cav)],
        unknowns=[f"u{i}" for i in range(unk)],
    )


class TestTailAllEmpty:
    def test_all_empty_is_true(self) -> None:
        assert tail_all_empty(_draft()) is True

    def test_single_field_present_is_false(self) -> None:
        assert tail_all_empty(_draft(unk=1)) is False


class _FakeLLM:
    def __init__(self, drafts: list[EventNewsDraft]) -> None:
        self._drafts = drafts
        self.calls = 0

    async def generate_structured(self, prompt: str, schema: type, **_kw: Any) -> EventNewsDraft:
        d = self._drafts[min(self.calls, len(self._drafts) - 1)]
        self.calls += 1
        return d


def _member() -> MemberArticle:
    return MemberArticle(
        article_id="a1",
        title="t",
        url="https://kuebiko.example/a1",
        feed_title="feed",
        feed_url="https://kuebiko.example/feed",
        host="kuebiko.example",
        importance="medium",
        category="apt",
        status="posted",
        anchor_ts=datetime(2026, 9, 1, tzinfo=UTC),
        summary="s",
        body="",
        entities=frozenset(),
    )


def _run(llm: _FakeLLM) -> EventNewsDraft:
    return asyncio.run(generate_draft([_member()], "", cast(LLMClient, llm)))


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVENTNEWS_TAIL_GATE", raising=False)


def test_non_empty_tail_is_not_resampled() -> None:
    llm = _FakeLLM([_draft(unk=1)])
    _run(llm)
    assert llm.calls == 1


def test_all_empty_tail_is_resampled_and_best_taken() -> None:
    llm = _FakeLLM([_draft(), _draft(cav=2, unk=1)])
    result = _run(llm)
    assert llm.calls == 2
    assert len(result.caveats) == 2 and result.unknowns == ["u0"]


def test_limit_reached_keeps_draft_and_passes_through() -> None:
    llm = _FakeLLM([_draft(), _draft(), _draft()])
    result = _run(llm)
    assert llm.calls == 1 + MAX_RESAMPLES
    assert tail_all_empty(result)  # 保存は止めない (記録のみ)


def test_gate_disabled_by_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENTNEWS_TAIL_GATE", "0")
    llm = _FakeLLM([_draft()])
    _run(llm)
    assert llm.calls == 1


class TestTruncatedDraftIsNotResampled:
    """⚠ **切り詰められた出力を再サンプルしない** (2026-09-21)。

    統合でできた大きな事象は出力上限 6,144 tok に張り付き、尾部 3 欄が書かれる前に
    切れる。尾部が空なのは「述べることが無かった」のではなく「そこまで届かなかった」
    ためで、同一プロンプトの再サンプルは同じ所で切れる (実測: 3 回とも上限到達、
    1 事象 8 分)。実測の裏付け (直近 14 日・248 版): 本文 p90 6,169 字 / 最大 7,678 字で、
    通常の生成は上限に当たらない (直近 30 日 3,062 版: facts 総字数 最大 5,321 字、
    6,000 字以上は 0 件)。
    """

    def test_large_draft_with_empty_tail_is_treated_as_truncated(self) -> None:
        from src.eventnews.tail_gate import looks_truncated

        big = EventNewsDraft(
            headline="h",
            bluf="b",
            facts=[FactItem(text=f"事実{i}" + "あ" * 300, source_index=1) for i in range(40)],
        )

        assert tail_all_empty(big) and looks_truncated(big)

    def test_ordinary_draft_with_empty_tail_is_not_truncated(self) -> None:
        from src.eventnews.tail_gate import looks_truncated

        assert not looks_truncated(_draft())


def test_truncated_draft_skips_resampling(monkeypatch: pytest.MonkeyPatch) -> None:
    """配線: 切り詰めと見なした draft は再サンプルせずそのまま通す。"""
    from src.eventnews import generator

    big = EventNewsDraft(
        headline="h",
        bluf="b",
        facts=[FactItem(text=f"事実{i}" + "あ" * 300, source_index=1) for i in range(40)],
    )
    calls = 0

    class _Llm:
        model = "fake"

        async def generate_structured(self, **kw: Any) -> EventNewsDraft:
            nonlocal calls
            calls += 1
            return big

    monkeypatch.setattr(generator, "build_prompt", lambda *a, **k: "P")
    out = asyncio.run(generate_draft([], "", cast(LLMClient, _Llm())))

    assert calls == 1  # 再サンプルしない (従来は 1 + MAX_RESAMPLES = 3 回)
    assert out.headline == "h"
