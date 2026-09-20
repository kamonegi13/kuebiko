"""src.digest.llm_digest の出力予算スケーリング (2026-07-07)。

件数駆動 recap: 選定件数に応じ digest 出力予算を拡張し各記事の厚みを保つ。
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from src.digest.llm_digest import (
    _DIGEST_MAX_TOKENS_CEILING,
    _DIGEST_TOKENS_PER_ITEM,
    DIGEST_MAX_TOKENS,
    _digest_max_tokens,
)


def test_floor_for_few_items() -> None:
    # 少数件は従来同等の floor (最低保証)
    assert _digest_max_tokens(1) == DIGEST_MAX_TOKENS
    assert _digest_max_tokens(5) == DIGEST_MAX_TOKENS


def test_scales_with_item_count() -> None:
    # floor を超える件数では per-item ぶん拡張し厚みを保つ
    n = (DIGEST_MAX_TOKENS // _DIGEST_TOKENS_PER_ITEM) + 3
    assert _digest_max_tokens(n) == n * _DIGEST_TOKENS_PER_ITEM


def test_capped_at_ceiling() -> None:
    # 青天井にはせず ceiling で頭打ち (生成時間/可読性)
    assert _digest_max_tokens(1000) == _DIGEST_MAX_TOKENS_CEILING


class TestGenerateDigestCoverage:
    """網羅は指示でなく構造で守る (2026-09-20)。

    旧構成は「入力記事は基本的にすべて含めよ (見逃し防止が最優先)」と書いてあったのに、
    直近 5 回のうち 3 回で 12 件選んで 3-5 件しか載っていなかった。**指示では止まらない**
    ので、漏れを検出して名指しで書き直させ、それでも漏れたら WARNING に残す。
    """

    @staticmethod
    def _cand(article_id: str) -> Any:
        from src.digest.db_filter import DigestCandidate

        return DigestCandidate(
            article_id=article_id,
            title=f"題 {article_id}",
            url=f"https://kuebiko.example/{article_id}",
            feed_title="Feed",
            summary="要約",
            importance="high",
            category="apt",
            posted_channel="watch",
            dedup_key=None,
            created_at="2026-09-20T00:00:00Z",
            discord_channel_id=None,
            discord_message_id=None,
        )

    @pytest.mark.asyncio
    async def test_missing_article_triggers_one_rewrite_naming_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.digest import llm_digest as mod
        from src.digest.recap_render import RecapOutput, RecapSection

        prompts: list[str] = []
        outs = [
            RecapOutput(sections=[RecapSection(heading="節", body="あ" * 600, article_ids=["a1"])]),
            RecapOutput(
                sections=[RecapSection(heading="節", body="あ" * 600, article_ids=["a1", "a2"])]
            ),
        ]

        async def fake(llm: Any, prompt: str, *, max_tokens: int, think: Any) -> RecapOutput:
            prompts.append(prompt)
            return outs[len(prompts) - 1]

        monkeypatch.setattr(mod, "_generate_sections", fake)
        monkeypatch.setattr(mod, "_render_prompt", lambda *a, **k: "PROMPT")

        md = await mod.generate_digest(
            llm=cast(Any, object()),
            candidates=[self._cand("a1"), self._cand("a2")],
            template_name="digest/weekly_recap.j2",
            period_label="P",
        )

        assert len(prompts) == 2  # 1 度だけ書き直す
        assert "a2" in prompts[1]  # どれが漏れたかを名指しする
        assert "https://kuebiko.example/a2" in md  # 書き直し後は出典に載る

    @pytest.mark.asyncio
    async def test_full_coverage_does_not_rewrite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.digest import llm_digest as mod
        from src.digest.recap_render import RecapOutput, RecapSection

        calls = 0

        async def fake(llm: Any, prompt: str, *, max_tokens: int, think: Any) -> RecapOutput:
            nonlocal calls
            calls += 1
            return RecapOutput(
                sections=[RecapSection(heading="節", body="あ" * 600, article_ids=["a1"])]
            )

        monkeypatch.setattr(mod, "_generate_sections", fake)
        monkeypatch.setattr(mod, "_render_prompt", lambda *a, **k: "PROMPT")

        await mod.generate_digest(
            llm=cast(Any, object()),
            candidates=[self._cand("a1")],
            template_name="digest/weekly_recap.j2",
            period_label="P",
        )

        assert calls == 1

    @pytest.mark.asyncio
    async def test_keeps_the_first_output_when_the_rewrite_regresses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """書き直しは悪化しうる。実測で網羅 3 件漏れ → 7 件漏れ + 空の節に退行した。"""
        from src.digest import llm_digest as mod
        from src.digest.recap_render import RecapOutput, RecapSection

        good = RecapOutput(
            sections=[RecapSection(heading="節", body="あ" * 600, article_ids=["a1"])]
        )
        worse = RecapOutput(sections=[RecapSection(heading="", body="", article_ids=[])])
        outs = [good, worse]

        async def fake(llm: Any, prompt: str, *, max_tokens: int, think: Any) -> RecapOutput:
            return outs.pop(0)

        monkeypatch.setattr(mod, "_generate_sections", fake)
        monkeypatch.setattr(mod, "_render_prompt", lambda *a, **k: "PROMPT")

        md = await mod.generate_digest(
            llm=cast(Any, object()),
            candidates=[self._cand("a1"), self._cand("a2")],
            template_name="digest/weekly_recap.j2",
            period_label="P",
        )

        assert "## 📌 節" in md  # 退行した方を採らない

    @pytest.mark.asyncio
    async def test_rewrite_prompt_shows_the_previous_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """前回の出力を見せずに「○○を直せ」と言っても、モデルはそれを知らない。"""
        from src.digest import llm_digest as mod
        from src.digest.recap_render import RecapOutput, RecapSection

        prompts: list[str] = []

        async def fake(llm: Any, prompt: str, *, max_tokens: int, think: Any) -> RecapOutput:
            prompts.append(prompt)
            return RecapOutput(
                sections=[RecapSection(heading="既存の節", body="あ" * 600, article_ids=["a1"])]
            )

        monkeypatch.setattr(mod, "_generate_sections", fake)
        monkeypatch.setattr(mod, "_render_prompt", lambda *a, **k: "PROMPT")

        await mod.generate_digest(
            llm=cast(Any, object()),
            candidates=[self._cand("a1"), self._cand("a2")],
            template_name="digest/weekly_recap.j2",
            period_label="P",
        )

        assert "既存の節" in prompts[1]  # 直す対象を渡している
