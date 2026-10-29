"""地政学の救済の prompt 選択 (GEO_RESCUE_PROMPT) と戦域の取り込みヒントのテスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.cti.ingest_relevance import geo_theatre_hint_mode, geo_theatre_reasons
from src.pipeline.filters import _rescue_geopolitical, geo_rescue_prompt_mode
from src.tools.article_model import Article
from src.tools.article_triage import ArticleTriage, TriageDecision, resolve_prompt_path


def _article(i: str = "a") -> Article:
    return Article(
        id=i,
        url=f"https://example.com/{i}",
        title="Houthi missile strike in the Red Sea",
        summary_html="",
        published=datetime(2026, 10, 30, tzinfo=UTC),
        feed_title="Feed",
        feed_url="https://example.com/feed",
    )


def test_prompt_path_is_flat_under_triage_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_FLAT", "1")
    assert resolve_prompt_path() == "flat"


def test_force_pir_overrides_triage_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_FLAT", "1")
    monkeypatch.setenv("PIR_DRIVEN_TRIAGE", "1")
    assert resolve_prompt_path(force_pir=True) == "pir"


def test_rescue_prompt_mode_defaults_to_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEO_RESCUE_PROMPT", raising=False)
    assert geo_rescue_prompt_mode() == "flat"
    monkeypatch.setenv("GEO_RESCUE_PROMPT", "PIR")
    assert geo_rescue_prompt_mode() == "pir"
    monkeypatch.setenv("GEO_RESCUE_PROMPT", "garbage")
    assert geo_rescue_prompt_mode() == "flat"


@pytest.mark.parametrize("mode,expected", [("flat", "flat"), ("pir", "pir")])
@pytest.mark.asyncio
async def test_rescue_uses_selected_prompt_path(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected: str
) -> None:
    monkeypatch.setenv("TRIAGE_FLAT", "1")
    monkeypatch.setenv("PIR_DRIVEN_TRIAGE", "1")
    monkeypatch.setenv("GEO_RESCUE_PROMPT", mode)
    seen: list[str] = []
    orig = ArticleTriage._build_prompt

    def _spy(self: ArticleTriage, article: Article) -> str:
        seen.append(self.prompt_path)
        return orig(self, article)

    monkeypatch.setattr(ArticleTriage, "_build_prompt", _spy)
    llm: Any = AsyncMock()
    llm.generate_structured.return_value = TriageDecision(importance="high", reason="r")
    decisions = [(_article(), "low", False, "地政学のため")]

    out, rescued = await _rescue_geopolitical(
        decisions, llm, keep_importance={"medium", "high"}, think=False
    )

    assert seen == [expected]
    assert rescued == {"a"}
    assert out[0][1] == "medium"  # high でも medium に抑える


def test_theatre_reasons_match_en_and_ja() -> None:
    assert "theatre:red_sea" in geo_theatre_reasons("Houthi attack on Red Sea shipping")
    assert "theatre:senkaku" in geo_theatre_reasons("尖閣諸島周辺の動き")
    assert geo_theatre_reasons("Quarterly earnings beat expectations") == ()


def test_theatre_hint_mode_default_is_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INGEST_GEO_THEATRE_HINT", raising=False)
    assert geo_theatre_hint_mode() == "shadow"
    monkeypatch.setenv("INGEST_GEO_THEATRE_HINT", "1")
    assert geo_theatre_hint_mode() == "on"
    monkeypatch.setenv("INGEST_GEO_THEATRE_HINT", "0")
    assert geo_theatre_hint_mode() == "off"
