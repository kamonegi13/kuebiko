"""薄い本文の 2 段目判定 (S4、2026-10-11) のテスト。

決定的な関門 (THIN_BODY_GATE、tests/unit/test_main.py::TestThinBodyGate) をすり抜けた
短い本文に、logprob 判定 (``src.tools.decision_probe.probe_choice``) でもう一段かける。
``probe_choice`` 自体は tests/unit/test_decision_probe.py で検証済みなのでここでは AsyncMock
で差し替え、呼び出し側 (briefing.py) の配線だけを検証する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.pipeline.briefing import (
    THIN_BODY_PROBE_MAX_BODY_CHARS,
    THIN_BODY_PROBE_THRESHOLD,
    DegenerateBodyError,
    _process_article,
    _thin_body_probe_reason,
)
from tests.unit.test_main import _article, _build_extractor_with_results, _extraction_success

_TITLE = "A" * 20
# 600 字未満 (THIN_BODY_PROBE_MAX_BODY_CHARS)・80 字以上 (body_too_short を回避) の短い本文。
_SHORT_BODY = _TITLE + "B" * 300
assert 80 <= len(_SHORT_BODY) < THIN_BODY_PROBE_MAX_BODY_CHARS


def _cfg(base_url: str = "http://localhost:11434") -> Any:
    class _Cfg:
        ollama_base_url = base_url

    return _Cfg()


# ---------- _thin_body_probe_reason ----------


class TestThinBodyProbeReason:
    def test_flag_unset_defaults_to_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("THIN_BODY_PROBE", raising=False)
        probe = AsyncMock(side_effect=AssertionError("呼ばれないはず"))
        monkeypatch.setattr("src.pipeline.briefing.probe_choice", probe)

        result = _run(
            _thin_body_probe_reason(_SHORT_BODY, title=_TITLE, article_id="rss:1", config=_cfg())
        )

        assert result is None
        probe.assert_not_called()

    def test_flag_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THIN_BODY_PROBE", "0")
        probe = AsyncMock(side_effect=AssertionError("呼ばれないはず"))
        monkeypatch.setattr("src.pipeline.briefing.probe_choice", probe)

        result = _run(
            _thin_body_probe_reason(_SHORT_BODY, title=_TITLE, article_id="rss:1", config=_cfg())
        )

        assert result is None
        probe.assert_not_called()

    def test_config_none_skips_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THIN_BODY_PROBE", "1")
        probe = AsyncMock(side_effect=AssertionError("呼ばれないはず"))
        monkeypatch.setattr("src.pipeline.briefing.probe_choice", probe)

        result = _run(
            _thin_body_probe_reason(_SHORT_BODY, title=_TITLE, article_id="rss:1", config=None)
        )

        assert result is None
        probe.assert_not_called()

    def test_long_body_skips_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THIN_BODY_PROBE", "1")
        probe = AsyncMock(side_effect=AssertionError("呼ばれないはず"))
        monkeypatch.setattr("src.pipeline.briefing.probe_choice", probe)
        long_body = _TITLE + "C" * THIN_BODY_PROBE_MAX_BODY_CHARS

        result = _run(
            _thin_body_probe_reason(long_body, title=_TITLE, article_id="rss:1", config=_cfg())
        )

        assert result is None
        probe.assert_not_called()

    def test_grok_article_skips_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THIN_BODY_PROBE", "1")
        probe = AsyncMock(side_effect=AssertionError("呼ばれないはず"))
        monkeypatch.setattr("src.pipeline.briefing.probe_choice", probe)

        result = _run(
            _thin_body_probe_reason(
                _SHORT_BODY, title=_TITLE, article_id="grok:12345", config=_cfg()
            )
        )

        assert result is None
        probe.assert_not_called()

    def test_probe_none_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # probe_choice 自体の失敗・外部 LLM 割当時は None (決定的な経路へそのまま戻る)。
        monkeypatch.setenv("THIN_BODY_PROBE", "1")
        probe = AsyncMock(return_value=None)
        monkeypatch.setattr("src.pipeline.briefing.probe_choice", probe)

        result = _run(
            _thin_body_probe_reason(_SHORT_BODY, title=_TITLE, article_id="rss:1", config=_cfg())
        )

        assert result is None
        probe.assert_awaited_once()

    def test_fires_when_p_content_below_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THIN_BODY_PROBE", "1")
        probe = AsyncMock(return_value={"Yes": THIN_BODY_PROBE_THRESHOLD - 0.01, "No": 1 - 0.065})
        monkeypatch.setattr("src.pipeline.briefing.probe_choice", probe)

        result = _run(
            _thin_body_probe_reason(_SHORT_BODY, title=_TITLE, article_id="rss:1", config=_cfg())
        )

        assert result == "thin_body_probe"

    def test_keeps_when_p_content_at_or_above_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("THIN_BODY_PROBE", "1")
        probe = AsyncMock(return_value={"Yes": THIN_BODY_PROBE_THRESHOLD, "No": 1 - 0.075})
        monkeypatch.setattr("src.pipeline.briefing.probe_choice", probe)

        result = _run(
            _thin_body_probe_reason(_SHORT_BODY, title=_TITLE, article_id="rss:1", config=_cfg())
        )

        assert result is None

    def test_missing_yes_key_is_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THIN_BODY_PROBE", "1")
        probe = AsyncMock(return_value={"No": 1.0})
        monkeypatch.setattr("src.pipeline.briefing.probe_choice", probe)

        result = _run(
            _thin_body_probe_reason(_SHORT_BODY, title=_TITLE, article_id="rss:1", config=_cfg())
        )

        assert result is None


# ---------- _process_article 配線 ----------


class TestProcessArticleWiring:
    @pytest.mark.asyncio
    async def test_raises_thin_body_probe_when_deterministic_gate_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THIN_BODY_GATE (既定 off) は発火しない状態でも、THIN_BODY_PROBE を立てれば
        # probe 経由で DegenerateBodyError(thin_body_probe) が起きる。
        monkeypatch.delenv("THIN_BODY_GATE", raising=False)
        monkeypatch.setenv("THIN_BODY_PROBE", "1")
        probe = AsyncMock(return_value={"Yes": 0.01, "No": 0.99})
        monkeypatch.setattr("src.pipeline.briefing.probe_choice", probe)

        article = _article(article_id="rss:thin", url="https://example.com/thin")
        extractor = _build_extractor_with_results([_extraction_success(_SHORT_BODY)])

        with pytest.raises(DegenerateBodyError) as exc_info:
            await _process_article(article, extractor, AsyncMock(), AsyncMock(), config=_cfg())

        assert exc_info.value.reason == "thin_body_probe"
        probe.assert_awaited_once()


def _run(coro: Any) -> Any:
    """asyncio.run の薄いラッパ (同期テストから async 関数を直接呼ぶため)。"""
    import asyncio

    return asyncio.run(coro)
