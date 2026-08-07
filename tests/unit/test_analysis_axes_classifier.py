"""analysis_axes_classifier (focused 分析軸分類器) の単体テスト。

summarizer 過負荷による末尾フィールド枯死 (intent/technical/event_date) の恒久修復。
editorial_stance_classifier と同じ「focused 単機能 + 障害時は安全側」の規約を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import jinja2
import pytest

from src.cti.analysis_axes_classifier import (
    AnalysisAxesOut,
    build_axes_prompt,
    classify_analysis_axes,
)
from src.cti.diamond_model import parse_diamond_axes


class TestBuildAxesPrompt:
    def test_includes_title_category_published(self) -> None:
        # Arrange / Act
        prompt = build_axes_prompt("T社に侵入", "apt", "本文 " * 100, None, "2026-07-13")

        # Assert
        assert prompt is not None
        assert "T社に侵入" in prompt
        assert "カテゴリ: apt" in prompt
        assert "報道日: 2026-07-13" in prompt

    def test_falls_back_to_summary_when_body_short(self) -> None:
        prompt = build_axes_prompt("t", None, "short", "要約テキスト " * 40, None)

        assert prompt is not None
        assert "要約テキスト" in prompt

    def test_returns_none_when_no_text(self) -> None:
        assert build_axes_prompt("t", "apt", "", "", None) is None

    def test_truncates_body_to_max_chars(self) -> None:
        prompt = build_axes_prompt("t", "apt", "x" * 20000, None, None)

        assert prompt is not None
        assert len(prompt) < 20000


class TestToDiamondDict:
    def test_roundtrips_through_parse_diamond_axes(self) -> None:
        # Arrange
        out = AnalysisAxesOut(
            intent="prepositioning",
            confidence="low",
            rationale="ツール類似のみ・帰属未確定",
            technical="VPN 装置経由で潜伏し LotL で横展開",
        )

        # Act
        axes = parse_diamond_axes(out.to_diamond_dict())

        # Assert
        assert axes.socio_political.intent == "prepositioning"
        assert axes.socio_political.confidence == "low"
        assert axes.technical == "VPN 装置経由で潜伏し LotL で横展開"

    def test_unknown_defaults_parse_to_unknown(self) -> None:
        axes = parse_diamond_axes(AnalysisAxesOut().to_diamond_dict())

        assert axes.socio_political.intent == "unknown"
        assert not axes.technical  # _clean_oneliner は空文字に倒す (falsy 判定で除外される)


class TestClassifyAnalysisAxes:
    @pytest.mark.asyncio
    async def test_returns_llm_output(self) -> None:
        # Arrange
        llm = AsyncMock()
        llm.generate_structured.return_value = AnalysisAxesOut(intent="espionage")

        # Act
        out = await classify_analysis_axes(
            llm, title="t", category="apt", body="本文 " * 100, summary_text=None, published=None
        )

        # Assert
        assert out is not None
        assert out.intent == "espionage"
        # think=False 明示 (digest 系 LLM 呼出の必須規約)
        assert llm.generate_structured.call_args.kwargs.get("think") is False

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_failure(self) -> None:
        llm = AsyncMock()
        llm.generate_structured.side_effect = RuntimeError("ollama down")

        out = await classify_analysis_axes(
            llm, title="t", category=None, body="本文 " * 100, summary_text=None, published=None
        )

        assert out is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_text(self) -> None:
        llm = AsyncMock()

        out = await classify_analysis_axes(
            llm, title="t", category=None, body="", summary_text="", published=None
        )

        assert out is None
        llm.generate_structured.assert_not_called()


class TestBriefingOverride:
    """forward hook: 分類器成功時は summarizer 由来の軸を上書き、失敗時は維持する。"""

    @pytest.fixture
    def template(self) -> jinja2.Template:
        env = jinja2.Environment(
            loader=jinja2.DictLoader({"s.j2": "{{ article.title }}\n{{ body }}"}),
            autoescape=False,
        )
        return env.get_template("s.j2")

    def _article(self) -> Any:
        from src.tools.article_model import Article

        return Article(
            id="a1",
            title="Sample",
            url="https://example.com/a",
            summary_html="<p>b</p>",
            author="x",
            published=datetime(2026, 7, 10, tzinfo=UTC),
            feed_title="Feed",
            feed_url="https://example.com/feed",
        )

    def _summary_output(self) -> Any:
        from src.pipeline.summary import SummaryOutput

        return SummaryOutput(
            title_ja="タイトル",
            importance="medium",
            category="apt",
            summary="要約本文。" * 10,
        )

    def _llm(self, axes: object) -> AsyncMock:
        """2026-07-26: briefing は判断軸を統合判断分類器 (JudgmentOut) 1 呼び出しで取得する。
        テストは AnalysisAxesOut を軸値の運搬に使い、mock が JudgmentOut に写像する。"""
        from types import SimpleNamespace

        from src.cti.judgment_classifier import JudgmentOut
        from src.pipeline.summary import SummaryOutput

        llm = AsyncMock()

        async def _gen(prompt: object, schema: object = None, **kw: object) -> object:
            if schema is SummaryOutput:
                return self._summary_output()
            if schema is JudgmentOut:
                if isinstance(axes, BaseException):
                    raise axes
                return JudgmentOut(
                    editorial_stance="factual_report",
                    intent=getattr(axes, "intent", "unknown"),
                    confidence=getattr(axes, "confidence", "low"),
                    rationale=getattr(axes, "rationale", None),
                    technical=getattr(axes, "technical", None),
                    event_date=getattr(axes, "event_date", None),
                    event_date_basis=getattr(axes, "event_date_basis", None),
                    compromise_date=getattr(axes, "compromise_date", None),
                    i_infra=getattr(axes, "i_infra", False),
                )
            return SimpleNamespace(editorial_stance="factual_report")

        llm.generate_structured.side_effect = _gen
        return llm

    @pytest.mark.asyncio
    async def test_axes_override_persisted_to_metadata(self, template: jinja2.Template) -> None:
        # Arrange
        from src.pipeline.briefing import _summarize_and_build

        axes = AnalysisAxesOut(
            intent="prepositioning",
            confidence="low",
            rationale="弱シグナル",
            technical="エッジ機器経由で潜伏",
            event_date="2026-07-08",
            event_date_basis="reported",
        )

        # Act
        msg = await _summarize_and_build(
            self._article(), "body text " * 100, self._llm(axes), template
        )

        # Assert
        assert msg.metadata["socio_political_intent"] == "prepositioning"
        assert msg.metadata["intent_confidence"] == "low"
        assert msg.metadata["technical_axis_summary"] == "エッジ機器経由で潜伏"
        assert msg.metadata["event_date"] == "2026-07-08"
        assert msg.metadata["event_date_basis"] == "reported"

    @pytest.mark.asyncio
    async def test_classifier_failure_keeps_summarizer_defaults(
        self, template: jinja2.Template
    ) -> None:
        # Arrange: 分類器が失敗 → summarizer 由来 (空) を維持 = 正直な欠測
        from src.pipeline.briefing import _summarize_and_build

        # Act
        msg = await _summarize_and_build(
            self._article(), "body text " * 100, self._llm(RuntimeError("down")), template
        )

        # Assert
        assert "socio_political_intent" not in msg.metadata
        assert "event_date" not in msg.metadata

    @pytest.mark.asyncio
    async def test_huge_body_truncated_for_llm_prompt(self, template: jinja2.Template) -> None:
        """巨大本文 (CISA/Siemens advisory 757KB 実測) が要約/判定 LLM プロンプトを溢れさせ
        Ollama 推論 timeout (300s) → RSS run partial_failure を起こすのを防ぐ (2026-07-29)。
        LLM に渡す body は MAX_LLM_BODY_CHARS で切り詰められる (IoC 抽出は full body)。"""
        from types import SimpleNamespace

        from src.cti.judgment_classifier import JudgmentOut
        from src.pipeline.briefing import MAX_LLM_BODY_CHARS, _summarize_and_build
        from src.pipeline.summary import SummaryOutput

        captured: list[str] = []
        llm = AsyncMock()

        async def _gen(prompt: object, schema: object = None, **kw: object) -> object:
            captured.append(str(prompt))
            if schema is SummaryOutput:
                return self._summary_output()
            if schema is JudgmentOut:
                return JudgmentOut(editorial_stance="factual_report")
            return SimpleNamespace(editorial_stance="factual_report")

        llm.generate_structured.side_effect = _gen

        # 800KB の本文 (実測の 757KB を超える) を渡す
        await _summarize_and_build(self._article(), "A" * 800_000, llm, template)

        # 全 LLM プロンプトの body が cap 済 (未修正なら ~800K で assert 失敗)
        assert captured, "LLM が呼ばれていない"
        for p in captured:
            assert len(p) < MAX_LLM_BODY_CHARS + 5_000, f"プロンプトが未切り詰め: {len(p)} 字"

    @pytest.mark.asyncio
    async def test_future_event_date_dropped_by_normalizer(self, template: jinja2.Template) -> None:
        # Arrange: 報道日 (2026-07-10) +1d を超える日付は既存 _normalize_temporal が捨てる
        from src.pipeline.briefing import _summarize_and_build

        axes = AnalysisAxesOut(
            intent="unknown", event_date="2026-08-01", event_date_basis="occurred"
        )

        # Act
        msg = await _summarize_and_build(
            self._article(), "body text " * 100, self._llm(axes), template
        )

        # Assert
        assert "event_date" not in msg.metadata
        assert "socio_political_intent" not in msg.metadata  # unknown は書かない


class TestFinalPmesiiAxes:
    """PMESII 軸の最終確定 (2026-07-16 監査対処): T 廃止 + I-infra 決定論フロア。"""

    def test_strips_retired_t_axis(self) -> None:
        from src.pipeline.briefing import _final_pmesii_axes

        out = _final_pmesii_axes(
            ["M", "T", "I-cyber"], sector_canonical=None, title="一般記事", summary_text=""
        )
        assert "T" not in out
        assert out == ["M", "I-cyber"]

    def test_adds_i_infra_via_deterministic_sector_floor(self) -> None:
        # Arrange: 医療セクター canonical → NISC CI 写像に該当 (LLM 供給ゼロでも立つ)
        from src.pipeline.briefing import _final_pmesii_axes

        out = _final_pmesii_axes(
            ["E"],
            sector_canonical="healthcare",
            title="病院がランサムウェア被害",
            summary_text="医療機関のシステムが停止した",
        )
        assert "I-infra" in out

    def test_no_duplicate_i_infra(self) -> None:
        from src.pipeline.briefing import _final_pmesii_axes

        out = _final_pmesii_axes(
            ["I-infra", "E"],
            sector_canonical="healthcare",
            title="病院がランサムウェア被害",
            summary_text="",
        )
        assert out.count("I-infra") == 1

    def test_non_ci_article_gets_no_floor(self) -> None:
        from src.pipeline.briefing import _final_pmesii_axes

        out = _final_pmesii_axes(
            ["E"],
            sector_canonical=None,
            title="小売企業の会員データ漏えい",
            summary_text="EC サイトの顧客情報が流出",
        )
        assert "I-infra" not in out

    def test_classifier_schema_accepts_i_infra(self) -> None:
        from src.cti.analysis_axes_classifier import AnalysisAxesOut

        out = AnalysisAxesOut.model_validate({"intent": "espionage", "i_infra": True})
        assert out.i_infra is True
        assert AnalysisAxesOut().i_infra is False
