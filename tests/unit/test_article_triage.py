"""src.tools.article_triage のテスト (Phase 3.1)。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from src.tools.article_model import Article
from src.tools.article_triage import ArticleTriage, TriageDecision


def _article(title: str = "Test", summary: str = "<p>summary</p>") -> Article:
    return Article(
        id="t:1",
        title=title,
        url="https://example.com/x",
        summary_html=summary,
        author=None,
        published=datetime.now(UTC),
        feed_title="Test Feed",
        feed_url="https://example.com/feed",
    )


@pytest.mark.asyncio
async def test_returns_high_importance_from_llm() -> None:
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="high", reason="国家APTの新キャンペーン"),
    )
    triage = ArticleTriage(llm)
    decision = await triage.triage(_article("中国APT Volt Typhoon 新事象"))
    assert decision.importance == "high"
    assert "国家APT" in decision.reason


@pytest.mark.asyncio
async def test_returns_low_for_unrelated() -> None:
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="low", reason="一般Tips"),
    )
    triage = ArticleTriage(llm)
    decision = await triage.triage(_article("パスワード管理のヒント"))
    assert decision.importance == "low"


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_medium() -> None:
    """LLM 失敗時は medium で graceful degradation (重要記事を捨てない)。"""
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(side_effect=RuntimeError("ollama down"))
    triage = ArticleTriage(llm)
    decision = await triage.triage(_article("X"))
    assert decision.importance == "medium"
    assert "triage error" in decision.reason


@pytest.mark.asyncio
async def test_prompt_includes_title_and_body() -> None:
    """プロンプトにタイトル・概要が含まれる (LLM 入力の確認)。"""
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="high", reason="x"),
    )
    triage = ArticleTriage(llm)
    await triage.triage(
        _article(
            title="CISA Warns of Telnet 0-day",
            summary="<p>The U.S. CISA issued a warning about CVE-2026-12345...</p>",
        ),
    )
    args = llm.generate_structured.call_args
    prompt = args[0][0]
    assert "CISA Warns of Telnet 0-day" in prompt
    assert "CVE-2026-12345" in prompt


@pytest.mark.asyncio
async def test_html_tags_stripped_from_preview() -> None:
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="medium", reason=""),
    )
    triage = ArticleTriage(llm)
    await triage.triage(_article(summary="<p>本文</p><div>もう一つ</div>"))
    prompt = llm.generate_structured.call_args[0][0]
    # HTML タグは除去されている
    assert "<p>" not in prompt
    assert "<div>" not in prompt
    # 本文テキストは残る
    assert "本文" in prompt
    assert "もう一つ" in prompt


@pytest.mark.asyncio
async def test_llm_invoked_with_think_false() -> None:
    """thinking モード OFF で呼び出される (要約タスク同様、思考不要)。"""
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="high", reason="x"),
    )
    triage = ArticleTriage(llm)
    await triage.triage(_article())
    args = llm.generate_structured.call_args
    assert args.kwargs.get("think") is False


# ---------- Phase 5P: triage 失敗の表面化 (error フラグ) ----------


def test_triage_decision_has_error_flag_default_false() -> None:
    decision = TriageDecision(importance="medium", reason="x")
    assert decision.error is False


@pytest.mark.asyncio
async def test_triage_returns_error_true_on_llm_failure() -> None:
    """LLM 失敗時は medium に倒すと同時に error=True を立てる (silent 失敗対策)。"""
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(side_effect=RuntimeError("ollama down"))
    triage = ArticleTriage(llm)
    decision = await triage.triage(_article("X"))
    assert decision.importance == "medium"
    assert decision.error is True
    assert "triage error" in decision.reason


@pytest.mark.asyncio
async def test_triage_returns_error_false_on_normal_path() -> None:
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="high", reason="x"),
    )
    triage = ArticleTriage(llm)
    decision = await triage.triage(_article("X"))
    assert decision.error is False


class TestFilterByTriageRejectedSplit:
    """_filter_by_triage の rejected/枠あふれ分離 (2026-07-12 リサイクル根治)。

    importance 不足 = 評価済み・不採用 → rejected (呼び出し側が URL 既読化する終端状態)。
    max_keep の枠あふれ = 評価は keep 水準 → rejected に含めない (次 run のリトライ権)。
    """

    @staticmethod
    def _a(aid: str, title: str) -> Article:
        return Article(
            id=aid,
            title=title,
            url=f"https://example.com/{aid}",
            summary_html="<p>s</p>",
            author=None,
            published=datetime.now(UTC),
            feed_title="Test Feed",
            feed_url="https://example.com/feed",
        )

    @pytest.mark.asyncio
    async def test_low_importance_goes_to_rejected(self) -> None:
        from src.pipeline.filters import _filter_by_triage

        arts = [self._a("a-high", "high article"), self._a("a-low", "low article")]
        decisions = {
            "high article": TriageDecision(importance="high", reason="x"),
            "low article": TriageDecision(importance="low", reason="x"),
        }
        llm = AsyncMock()

        async def _gen(prompt: str, *a: object, **k: object) -> TriageDecision:
            for t, d in decisions.items():
                if t in prompt:
                    return d
            return TriageDecision(importance="low", reason="?")

        llm.generate_structured = AsyncMock(side_effect=_gen)
        survivors, skipped, skipped_ids, _err, rejected, all_decisions = await _filter_by_triage(
            arts, llm, keep_importance={"high", "medium"}, max_keep=10
        )
        assert [a.id for a in survivors] == ["a-high"]
        assert skipped == 1
        assert skipped_ids == ["a-low"]
        assert [a.id for a in rejected] == ["a-low"]
        # ヘッド v0 シャドー記録 (§14.3) 用: 棄却分も含む全判定が露出される
        assert {(a.id, imp) for a, imp, _e in all_decisions} == {
            ("a-high", "high"),
            ("a-low", "low"),
        }

    @pytest.mark.asyncio
    async def test_max_keep_overflow_is_skipped_but_not_rejected(self) -> None:
        from src.pipeline.filters import _filter_by_triage

        arts = [self._a(f"a-{i}", f"high article {i}") for i in range(3)]
        llm = AsyncMock()
        llm.generate_structured = AsyncMock(
            return_value=TriageDecision(importance="high", reason="x"),
        )
        survivors, skipped, skipped_ids, _err, rejected, _decisions = await _filter_by_triage(
            arts, llm, keep_importance={"high", "medium"}, max_keep=2
        )
        assert len(survivors) == 2
        assert skipped == 1  # 枠あふれは skip には数える (既読化はしない)
        assert len(skipped_ids) == 1
        assert rejected == []  # 評価は keep 水準 → リトライ権を保持
