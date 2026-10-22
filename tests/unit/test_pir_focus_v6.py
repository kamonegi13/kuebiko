"""SIR 別の要点 v6.2 (s24c、2026-10-22)。

s24c のとき (または ``PIR_FOCUS_PROMPT_V6=1``) だけ v6.2 の文面・長い出力の許容に切り替わる。
**s23i のときは従来の経路が不変**であることをここで固定する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.digest import focus_v6
from src.digest.pir_daily_focus import (
    PirFocusSection,
    _generate_llm_summary,
    _score_claim_sentences,
    format_compact_digest,
    unwrap_json_text,
)
from src.pir.evaluator import PirMatch
from src.pir.models import Pir, SpotlightConfig, StrongSignals


def _pir() -> Pir:
    return Pir(
        id="pir_x",
        title="テスト SIR",
        description="説明",
        enabled=True,
        strong_signals=StrongSignals(keywords=["k"]),
        spotlight=SpotlightConfig(enabled=False),
    )


def _match(i: int = 1) -> PirMatch:
    return PirMatch(
        article_id=f"a{i}",
        title=f"Title {i}",
        url=f"https://example.com/{i}",
        feed_title="Feed",
        importance="medium",
        posted_channel="brief",
        created_at="2026-05-25T05:00:00+00:00",
        matched_via=("keywords",),
        summary="要約です。",
    )


def _llm(model: str, text: str) -> Any:
    llm = AsyncMock()
    llm.model = model
    llm.generate = AsyncMock(return_value=type("R", (), {"text": text})())
    return llm


class TestIsEnabled:
    def test_s24c_family_enabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(focus_v6.FLAG_ENV, raising=False)
        for model in ("kuebiko-sft:s24c", "kuebiko-sft:s24ci50"):
            assert focus_v6.is_enabled(model) is True

    def test_other_models_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(focus_v6.FLAG_ENV, raising=False)
        for model in ("kuebiko-sft:s23i", "kuebiko-sft:s24", "kuebiko-sft:s24b", "gemma4:26b", ""):
            assert focus_v6.is_enabled(model) is False

    def test_flag_one_forces_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(focus_v6.FLAG_ENV, "1")
        assert focus_v6.is_enabled("kuebiko-sft:s23i") is True

    def test_flag_zero_forces_off_even_for_s24c(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(focus_v6.FLAG_ENV, "0")
        assert focus_v6.is_enabled("kuebiko-sft:s24c") is False


class TestPromptV6:
    def test_renders_v62_wording_without_length_limit(self) -> None:
        prompt = focus_v6.build_prompt(_pir(), [_match(1), _match(2)])
        assert prompt is not None
        assert "1 件も落とさない" in prompt
        assert "本 SIR には該当しない" in prompt
        assert "190 字以内" not in prompt
        assert "80-160 字" not in prompt
        assert "Title 1" in prompt and "Title 2" in prompt and "pir_x" in prompt

    def test_yaml_rubric_is_valid_for_composer(self) -> None:
        assert focus_v6.load_template() is not None


class TestGenerateWiring:
    @pytest.mark.asyncio
    async def test_s23i_uses_legacy_prompt_and_short_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(focus_v6.FLAG_ENV, raising=False)
        monkeypatch.setattr("src.digest.pir_daily_focus._build_prompt", lambda p, m: "LEGACY")
        llm = _llm("kuebiko-sft:s23i", "既知の手口での侵入が続いた。")
        out = await _generate_llm_summary(llm, _pir(), [_match()])
        assert out == "既知の手口での侵入が続いた。"
        kwargs = llm.generate.await_args.kwargs
        assert kwargs["prompt"] == "LEGACY"
        assert kwargs["max_tokens"] == 256

    @pytest.mark.asyncio
    async def test_s24c_uses_v6_prompt_and_long_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(focus_v6.FLAG_ENV, raising=False)
        monkeypatch.setattr("src.digest.pir_daily_focus._build_prompt", lambda p, m: "LEGACY")
        long_text = "Title 1 では侵入が確認された。" * 30
        llm = _llm("kuebiko-sft:s24c", long_text)
        out = await _generate_llm_summary(llm, _pir(), [_match()])
        assert out == long_text  # 長い本文を切らない
        kwargs = llm.generate.await_args.kwargs
        assert "1 件も落とさない" in kwargs["prompt"]
        assert kwargs["max_tokens"] == focus_v6.MAX_TOKENS > 256

    @pytest.mark.asyncio
    async def test_flag_forces_v6_on_s23i(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(focus_v6.FLAG_ENV, "1")
        llm = _llm("kuebiko-sft:s23i", "Title 1 の件。")
        await _generate_llm_summary(llm, _pir(), [_match()])
        assert "1 件も落とさない" in llm.generate.await_args.kwargs["prompt"]

    @pytest.mark.asyncio
    async def test_v6_accepts_not_applicable_statement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(focus_v6.FLAG_ENV, raising=False)
        text = "Title 1 は本 SIR には該当しない。Title 2 では国内企業の被害が確認された。"
        llm = _llm("kuebiko-sft:s24c", text)
        assert await _generate_llm_summary(llm, _pir(), [_match(1), _match(2)]) == text


class TestLenientUnwrap:
    def test_default_unwrap_unchanged_for_multi_key_json(self) -> None:
        raw = '{"article_id": "Feed", "summary": "本文です。"}'
        assert unwrap_json_text(raw) == raw  # s23i の経路は従来のまま

    def test_single_key_unchanged(self) -> None:
        assert unwrap_json_text('{"summary": "本文です。"}') == "本文です。"

    def test_lenient_unwraps_s24b_style_wrappers(self) -> None:
        cases = {
            '{"article_id": "Feed", "summary": "本文です。"}': "本文です。",
            '{"match_status": "該当", "summary": "本文です。"}': "本文です。",
            '{"pir_focus": "本文です。"}': "本文です。",
        }
        for raw, expected in cases.items():
            assert focus_v6.unwrap_lenient(raw) == expected

    def test_lenient_keeps_unknown_json_and_plain_text(self) -> None:
        assert focus_v6.unwrap_lenient("普通の本文。") == "普通の本文。"
        raw = '{"a": 1, "b": 2}'
        assert focus_v6.unwrap_lenient(raw) == raw

    @pytest.mark.asyncio
    async def test_s24c_json_wrapped_output_is_unwrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(focus_v6.FLAG_ENV, raising=False)
        llm = _llm("kuebiko-sft:s24c", '{"article_id": "Feed", "summary": "Title 1 の件。"}')
        assert await _generate_llm_summary(llm, _pir(), [_match()]) == "Title 1 の件。"


class TestNotApplicableSentence:
    def test_detects_markers(self) -> None:
        assert focus_v6.is_not_applicable("この記事は本 SIR には該当しない")
        assert not focus_v6.is_not_applicable("国内企業で侵害が確認された")

    @pytest.mark.asyncio
    async def test_claim_gate_does_not_judge_not_applicable_sentences(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe = AsyncMock(return_value={"Yes": 0.0, "No": 1.0})
        monkeypatch.setattr("src.digest.pir_daily_focus.probe_choice", probe)
        passed, failed = await _score_claim_sentences(
            "Title 1 は本 SIR には該当しない。",
            [_match()],
            config=object(),  # type: ignore[arg-type]
        )
        assert passed == ["Title 1 は本 SIR には該当しない"] and failed == []
        assert probe.await_count == 0


class TestDisplayWithLongSummary:
    def _section(self, summary: str) -> PirFocusSection:
        return PirFocusSection(
            pir=_pir(), matches=[_match()], total_match_count=1, llm_summary=summary
        )

    def test_discord_v2_gist_is_one_applicable_sentence(self) -> None:
        from src.digest.discord_brief import _focus_gist

        summary = (
            "Title 9 は本 SIR には該当しない。"
            "Title 1 では国内の製造業で侵害が確認された。" + "詳細な事実。" * 80
        )
        gist = _focus_gist({"summary": summary, "matches": []})
        assert gist == "Title 1 では国内の製造業で侵害が確認された。"

    def test_discord_v2_gist_unchanged_for_short_v5_text(self) -> None:
        from src.digest.discord_brief import _focus_gist

        assert _focus_gist({"summary": "既知の手口。補足。", "matches": []}) == "既知の手口。"

    def test_compact_digest_line_stays_bounded(self) -> None:
        out = format_compact_digest([self._section("長い要点。" * 200)], "2026-10-22")
        assert max(len(line) for line in out.splitlines()) < 260
