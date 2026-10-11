"""PIR daily focus 要点の文単位裏付け判定 (S6、2026-10-11) のテスト。

``src.tools.decision_probe.probe_choice`` (logprob 判定部品) 自体は
tests/unit/test_decision_probe.py で検証済みなので、ここでは AsyncMock で差し替え、
呼び出し側 (``pir_daily_focus.py``) の配線 — 文分解・しきい値判定・1 回だけの作り直し・
落とした文の除去 — だけを検証する (tests/unit/test_thin_body_probe.py と同じ流儀)。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.digest.pir_daily_focus import (
    PIR_FOCUS_CLAIM_THRESHOLD,
    apply_claim_gate,
    claim_gate_enabled,
    collect_pir_focus_sections,
    run_pir_daily_focus,
    split_claim_sentences,
)
from src.pir.models import PirConfig
from tests.unit.test_pir_daily_focus import _make_match, _make_pir


def _cfg(base_url: str = "http://localhost:11434") -> Any:
    class _Cfg:
        ollama_base_url = base_url

    return _Cfg()


# ---------- split_claim_sentences ----------


class TestSplitClaimSentences:
    def test_splits_on_japanese_period(self) -> None:
        assert split_claim_sentences("文1。文2。") == ["文1", "文2"]

    def test_drops_empty_parts(self) -> None:
        assert split_claim_sentences("文1。。文2。") == ["文1", "文2"]

    def test_strips_newlines(self) -> None:
        assert split_claim_sentences("文1。\n文2。") == ["文1", "文2"]

    def test_empty_text_returns_empty_list(self) -> None:
        assert split_claim_sentences("") == []


# ---------- claim_gate_enabled ----------


class TestClaimGateEnabled:
    def test_unset_defaults_to_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PIR_FOCUS_CLAIM_GATE", raising=False)
        assert claim_gate_enabled() is False

    def test_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PIR_FOCUS_CLAIM_GATE", "0")
        assert claim_gate_enabled() is False

    def test_one_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PIR_FOCUS_CLAIM_GATE", "1")
        assert claim_gate_enabled() is True


# ---------- apply_claim_gate ----------


class TestApplyClaimGate:
    def _pir_and_matches(self) -> tuple[Any, list[Any]]:
        pir = _make_pir()
        matches = [_make_match(title="侵入事案", feed_title="Feed")]
        return pir, matches

    @pytest.mark.asyncio
    async def test_flag_unset_defaults_to_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PIR_FOCUS_CLAIM_GATE", raising=False)
        probe = AsyncMock(side_effect=AssertionError("呼ばれないはず"))
        monkeypatch.setattr("src.digest.pir_daily_focus.probe_choice", probe)
        pir, matches = self._pir_and_matches()

        result = await apply_claim_gate(
            "文1。", llm=AsyncMock(), pir=pir, matches=matches, config=_cfg()
        )

        assert result == "文1。"
        probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_config_none_skips_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PIR_FOCUS_CLAIM_GATE", "1")
        probe = AsyncMock(side_effect=AssertionError("呼ばれないはず"))
        monkeypatch.setattr("src.digest.pir_daily_focus.probe_choice", probe)
        pir, matches = self._pir_and_matches()

        result = await apply_claim_gate(
            "文1。", llm=AsyncMock(), pir=pir, matches=matches, config=None
        )

        assert result == "文1。"
        probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_text_skips_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PIR_FOCUS_CLAIM_GATE", "1")
        probe = AsyncMock(side_effect=AssertionError("呼ばれないはず"))
        monkeypatch.setattr("src.digest.pir_daily_focus.probe_choice", probe)
        pir, matches = self._pir_and_matches()

        result = await apply_claim_gate(
            "", llm=AsyncMock(), pir=pir, matches=matches, config=_cfg()
        )

        assert result == ""
        probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_sentences_above_threshold_returns_text_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIR_FOCUS_CLAIM_GATE", "1")
        probe = AsyncMock(return_value={"Yes": PIR_FOCUS_CLAIM_THRESHOLD, "No": 0.0})
        monkeypatch.setattr("src.digest.pir_daily_focus.probe_choice", probe)
        llm = AsyncMock()
        llm.generate = AsyncMock(side_effect=AssertionError("再生成は呼ばれないはず"))
        pir, matches = self._pir_and_matches()

        result = await apply_claim_gate(
            "文1。文2。", llm=llm, pir=pir, matches=matches, config=_cfg()
        )

        assert result == "文1。文2。"
        llm.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_none_is_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # probe_choice 自体が通信失敗・外部 LLM 割当時に None を返す場合、その文は
        # 判定不能として通す (落とさない)。
        monkeypatch.setenv("PIR_FOCUS_CLAIM_GATE", "1")
        probe = AsyncMock(return_value=None)
        monkeypatch.setattr("src.digest.pir_daily_focus.probe_choice", probe)
        llm = AsyncMock()
        llm.generate = AsyncMock(side_effect=AssertionError("再生成は呼ばれないはず"))
        pir, matches = self._pir_and_matches()

        result = await apply_claim_gate("文1。", llm=llm, pir=pir, matches=matches, config=_cfg())

        assert result == "文1。"
        llm.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_failing_sentence_triggers_one_retry_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIR_FOCUS_CLAIM_GATE", "1")
        # 初回判定: 下回る → 再生成後の判定: 上回る
        probe = AsyncMock(
            side_effect=[
                {"Yes": PIR_FOCUS_CLAIM_THRESHOLD - 0.01, "No": 1.0},  # 初回: 失格
                {"Yes": PIR_FOCUS_CLAIM_THRESHOLD, "No": 0.0},  # 再生成後: 合格
            ]
        )
        monkeypatch.setattr("src.digest.pir_daily_focus.probe_choice", probe)
        llm = AsyncMock()
        llm.generate = AsyncMock(
            return_value=type("R", (), {"text": "作り直した文。"})(),
        )
        pir, matches = self._pir_and_matches()

        result = await apply_claim_gate(
            "元の文。", llm=llm, pir=pir, matches=matches, config=_cfg()
        )

        assert result == "作り直した文。"
        assert llm.generate.await_count == 1

    @pytest.mark.asyncio
    async def test_retry_still_failing_drops_only_that_sentence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIR_FOCUS_CLAIM_GATE", "1")
        high = {"Yes": PIR_FOCUS_CLAIM_THRESHOLD, "No": 0.0}
        low = {"Yes": PIR_FOCUS_CLAIM_THRESHOLD - 0.01, "No": 1.0}
        probe = AsyncMock(
            side_effect=[
                low,  # 初回「文1。」判定 → 失格 (文2 の判定前に短絡しないよう 2 文用意)
                high,  # 再生成後「作り直し文A。」判定 → 合格
                low,  # 再生成後「作り直し文B。」判定 → 失格
            ]
        )
        monkeypatch.setattr("src.digest.pir_daily_focus.probe_choice", probe)
        llm = AsyncMock()
        llm.generate = AsyncMock(
            return_value=type("R", (), {"text": "作り直し文A。作り直し文B。"})(),
        )
        pir, matches = self._pir_and_matches()

        result = await apply_claim_gate("文1。", llm=llm, pir=pir, matches=matches, config=_cfg())

        assert result == "作り直し文A。"
        assert llm.generate.await_count == 1

    @pytest.mark.asyncio
    async def test_all_sentences_still_failing_after_retry_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIR_FOCUS_CLAIM_GATE", "1")
        low = {"Yes": PIR_FOCUS_CLAIM_THRESHOLD - 0.01, "No": 1.0}
        probe = AsyncMock(return_value=low)
        monkeypatch.setattr("src.digest.pir_daily_focus.probe_choice", probe)
        llm = AsyncMock()
        llm.generate = AsyncMock(
            return_value=type("R", (), {"text": "作り直した文。"})(),
        )
        pir, matches = self._pir_and_matches()

        result = await apply_claim_gate(
            "元の文。", llm=llm, pir=pir, matches=matches, config=_cfg()
        )

        assert result == ""
        assert llm.generate.await_count == 1

    @pytest.mark.asyncio
    async def test_retry_generation_failure_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIR_FOCUS_CLAIM_GATE", "1")
        low = {"Yes": PIR_FOCUS_CLAIM_THRESHOLD - 0.01, "No": 1.0}
        probe = AsyncMock(return_value=low)
        monkeypatch.setattr("src.digest.pir_daily_focus.probe_choice", probe)
        llm = AsyncMock()
        # _generate_llm_summary の失敗 (LLM 例外) は既存仕様で "" を返す。
        llm.generate = AsyncMock(side_effect=RuntimeError("ollama down"))
        pir, matches = self._pir_and_matches()

        result = await apply_claim_gate(
            "元の文。", llm=llm, pir=pir, matches=matches, config=_cfg()
        )

        assert result == ""
        assert llm.generate.await_count == 1


# ---------- collect_pir_focus_sections / run_pir_daily_focus 配線 ----------


class TestCollectSectionsWiring:
    """config を渡したときだけ claim gate が section 組み立てに乗ることを確かめる。"""

    @pytest.mark.asyncio
    async def test_config_none_keeps_raw_llm_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PIR_FOCUS_CLAIM_GATE", "1")
        probe = AsyncMock(return_value={"Yes": PIR_FOCUS_CLAIM_THRESHOLD - 0.01, "No": 1.0})
        monkeypatch.setattr("src.digest.pir_daily_focus.probe_choice", probe)

        pir = _make_pir(pir_id="pir_a")
        pir_config = PirConfig(priorities=[pir])
        llm = AsyncMock()
        llm.generate = AsyncMock(return_value=type("R", (), {"text": "生の要点。"})())

        with (
            patch("src.digest.pir_daily_focus.load_current_pir_config", return_value=pir_config),
            patch(
                "src.digest.pir_daily_focus.evaluate_pir_matches",
                return_value=[_make_match(article_id="a1", importance="high")],
            ),
        ):
            sections = await collect_pir_focus_sections(llm=llm, lookback_hours=24, config=None)

        # config 省略時は claim gate 無効 (probe_choice 自体が呼ばれない)。
        assert sections[0].llm_summary == "生の要点。"
        probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_config_present_applies_claim_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PIR_FOCUS_CLAIM_GATE", "1")
        # 1 文のみの要点、しきい値未満 → 作り直し (再生成は同じ llm.generate を再度呼ぶ)。
        low = {"Yes": PIR_FOCUS_CLAIM_THRESHOLD - 0.01, "No": 1.0}
        high = {"Yes": PIR_FOCUS_CLAIM_THRESHOLD, "No": 0.0}
        probe = AsyncMock(side_effect=[low, high])
        monkeypatch.setattr("src.digest.pir_daily_focus.probe_choice", probe)

        pir = _make_pir(pir_id="pir_a")
        pir_config = PirConfig(priorities=[pir])
        llm = AsyncMock()
        llm.generate = AsyncMock(
            side_effect=[
                type("R", (), {"text": "裏付けなしの文。"})(),
                type("R", (), {"text": "作り直した文。"})(),
            ]
        )

        with (
            patch("src.digest.pir_daily_focus.load_current_pir_config", return_value=pir_config),
            patch(
                "src.digest.pir_daily_focus.evaluate_pir_matches",
                return_value=[_make_match(article_id="a1", importance="high")],
            ),
        ):
            sections = await collect_pir_focus_sections(llm=llm, lookback_hours=24, config=_cfg())

        assert sections[0].llm_summary == "作り直した文。"
        assert llm.generate.await_count == 2


@pytest.mark.asyncio
async def test_run_pir_daily_focus_passes_config_to_collect_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config が collect_pir_focus_sections まで届くことを配線レベルで確かめる。"""
    collect_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("src.digest.pir_daily_focus.collect_pir_focus_sections", collect_mock)

    config = _cfg()
    pipeline = type(
        "P",
        (),
        {"name": "pir-daily-focus", "source": type("S", (), {"digest_lookback_hours": 24})()},
    )()

    await run_pir_daily_focus(
        config=config, pipeline=pipeline, llm=AsyncMock(), publishers={}, dry_run=True
    )

    collect_mock.assert_awaited_once()
    await_args = collect_mock.await_args
    assert await_args is not None
    assert await_args.kwargs["config"] is config
