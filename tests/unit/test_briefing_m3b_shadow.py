"""M3b 段1 (2026-10-10): ``_summarize_and_build`` / ``_build_briefing`` の shadow 経路。

- 深刻度の軸 inline LLM 呼び出しは importance 帯 + サイバー category のときだけ発火する
- routing_v2_shadow metadata の組み立て失敗は投稿処理 (呼び出し元) を止めない
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import jinja2
import pytest

from src.cti.routing_decision import RoutingDecision
from src.cti.routing_signals import RoutingSignals
from src.tools.article_model import Article
from src.tools.discord_publisher import BriefingMessage


@pytest.fixture
def template() -> jinja2.Template:
    env = jinja2.Environment(
        loader=jinja2.DictLoader({"s.j2": "{{ article.title }}\n{{ body }}"}),
        autoescape=False,
    )
    return env.get_template("s.j2")


def _article() -> Article:
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


def _llm_for(*, importance: str, category: str, severity_axes_called: list[bool]) -> AsyncMock:
    from src.cti.judgment_classifier import JudgmentOut
    from src.cti.severity_axes import SeverityAxes
    from src.pipeline.summary import SummaryOutput

    llm = AsyncMock()
    llm.model = "fake-model"

    async def _gen(prompt: object, schema: object = None, **kw: object) -> object:
        if schema is SummaryOutput:
            return SummaryOutput(
                title_ja="タイトル",
                importance=importance,  # type: ignore[arg-type]
                category=category,
                summary="要約本文。" * 10,
            )
        if schema is JudgmentOut:
            return JudgmentOut(editorial_stance="factual_report")
        if schema is SeverityAxes:
            severity_axes_called.append(True)
            return SeverityAxes(
                scope="single_org",
                impact="data_exposure",
                confirmation="confirmed",
                magnitude="unknown",
                exploitation="not_applicable",
                actor="criminal",
                target="general_org",
            )
        return SimpleNamespace(editorial_stance="factual_report")

    llm.generate_structured.side_effect = _gen
    return llm


class TestInlineAxesEligibilityGating:
    async def test_called_when_importance_medium_and_cyber_category(
        self, template: jinja2.Template, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.pipeline.briefing import _summarize_and_build

        called: list[bool] = []
        llm = _llm_for(importance="medium", category="apt", severity_axes_called=called)
        # 軸は Step.SEVERITY_AXES の client で取る (要約の client を流用しない)。
        monkeypatch.setattr("src.cti.importance_v2_inline._axes_llm", lambda: llm)

        msg = await _summarize_and_build(_article(), "body text " * 100, llm, template)

        assert called, "importance=medium かつ category=apt (サイバー) は呼ばれるはず"
        assert msg.metadata.get("_severity_axes") is not None
        assert msg.metadata["_severity_axes"]["impact"] == "data_exposure"
        assert msg.metadata.get("_severity_axes_model") == "fake-model"

    async def test_not_called_when_importance_low(self, template: jinja2.Template) -> None:
        from src.pipeline.briefing import _summarize_and_build

        called: list[bool] = []
        llm = _llm_for(importance="low", category="apt", severity_axes_called=called)

        msg = await _summarize_and_build(_article(), "body text " * 100, llm, template)

        assert not called, "importance=low は毎時の段と同じ帯外 (呼ばれない)"
        assert "_severity_axes" not in msg.metadata

    async def test_not_called_when_category_is_non_cyber(self, template: jinja2.Template) -> None:
        from src.pipeline.briefing import _summarize_and_build

        called: list[bool] = []
        llm = _llm_for(importance="high", category="geopolitical", severity_axes_called=called)

        msg = await _summarize_and_build(_article(), "body text " * 100, llm, template)

        assert not called, "非サイバー category は derive_severity が常に None を返すため無駄"
        assert "_severity_axes" not in msg.metadata

    async def test_not_called_when_flag_disabled(
        self, template: jinja2.Template, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.pipeline.briefing import _summarize_and_build

        monkeypatch.setenv("ROUTING_V2_INLINE_AXES", "0")
        called: list[bool] = []
        llm = _llm_for(importance="high", category="apt", severity_axes_called=called)

        msg = await _summarize_and_build(_article(), "body text " * 100, llm, template)

        assert not called
        assert "_severity_axes" not in msg.metadata


class TestShadowMetadataFailureIsolation:
    """``_build_routing_v2_shadow_metadata`` の失敗は投稿処理を止めない (空 dict に倒す)。"""

    def _signals(self) -> RoutingSignals:
        return RoutingSignals(source="briefing", importance="high", article_type="breaking")

    def _decision(self) -> RoutingDecision:
        return RoutingDecision(channel="watch", rule_id="R7.fallback", reason="fallback")

    def _msg(self) -> BriefingMessage:
        return BriefingMessage(
            title="t", bluf="", summary="s", importance="high", category="breach", metadata={}
        )

    def test_build_importance_v2_inline_failure_yields_empty_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.pipeline import briefing as briefing_module

        def _boom(**_kw: object) -> Any:
            raise RuntimeError("inline importance v2 計算の失敗 (テスト注入)")

        monkeypatch.setattr("src.cti.importance_v2_inline.build_importance_v2_inline", _boom)

        result = briefing_module._build_routing_v2_shadow_metadata(
            signals=self._signals(),
            decision=self._decision(),
            msg=self._msg(),
            body_text="body",
            feed_title="feed",
            severity_axes=None,
            importance="high",
            article_id="a1",
        )

        assert result == {}

    def test_success_path_returns_shadow_metadata(self) -> None:
        from src.pipeline import briefing as briefing_module

        result = briefing_module._build_routing_v2_shadow_metadata(
            signals=self._signals(),
            decision=self._decision(),
            msg=self._msg(),
            body_text="body",
            feed_title="feed",
            severity_axes=None,
            importance="high",
            article_id="a1",
        )

        assert "_routing_v2_shadow" in result
        shadow = result["_routing_v2_shadow"]
        assert isinstance(shadow, dict)
        assert shadow["old_channel"] == "watch"
        assert shadow["old_rule_id"] == "R7.fallback"
        assert shadow["importance"] == "high"
        assert "new_channel" in shadow
