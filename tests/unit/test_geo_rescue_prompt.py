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


def test_public_news_treats_analysis_only_members_as_not_delivered() -> None:
    from types import SimpleNamespace

    from src.ui.api.public_news import _is_duplicate_only

    # Arrange
    arts: dict[str, Any] = {
        "ana": SimpleNamespace(status="posted", duplicate_of=None, analysis_only="geo_rescue"),
        "ok": SimpleNamespace(status="posted", duplicate_of=None, analysis_only=None),
    }

    # Act / Assert
    assert _is_duplicate_only(arts, ("ana",)) is True
    assert _is_duplicate_only(arts, ("ana", "ok")) is False


@pytest.mark.asyncio
async def test_rescue_with_force_pir_goes_through_composed_prompt_with_boundary_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """救済 (pir) は triage_rubric (boundary_rules 込み) を合成する経路を通り、flat は通らない。"""
    import src.tools.article_triage as mod

    # Arrange
    monkeypatch.setenv("TRIAGE_FLAT", "1")
    monkeypatch.setenv("PIR_DRIVEN_TRIAGE", "1")
    calls: list[dict[str, str]] = []

    def _fake(context: dict[str, str]) -> str:
        calls.append(context)
        return "COMPOSED"

    monkeypatch.setattr(mod, "_composed_prompt", _fake)
    monkeypatch.setattr("src.pir.integration.build_triage_high_criteria", lambda *_a, **_k: "HIGH")
    llm: Any = AsyncMock()

    # Act
    pir_prompt = ArticleTriage(llm, force_pir=True)._build_prompt(_article())
    flat_prompt = ArticleTriage(llm)._build_prompt(_article())

    # Assert
    assert pir_prompt == "COMPOSED" and len(calls) == 1
    assert flat_prompt != "COMPOSED" and len(calls) == 1


def test_mcnemar_and_strata_summary() -> None:
    from src.eval.geo_rescue_stats import mcnemar_exact, summarize

    # Arrange
    records = [
        {"id": "a", "flat_rescued": False, "pir_rescued": True},
        {"id": "b", "flat_rescued": False, "pir_rescued": True},
        {"id": "c", "flat_rescued": True, "pir_rescued": True},
    ]
    strata = {"a": "地政学", "b": "軍事", "c": "地政学"}
    gold = {"a": True, "b": True, "c": True}

    # Act
    out = summarize(records, strata, gold)

    # Assert
    assert out["per_stratum"]["地政学"] == {"n": 2, "flat_rescued": 1, "pir_rescued": 2}
    assert (out["flat_only_correct"], out["pir_only_correct"]) == (0, 2)
    assert out["mcnemar_p"] == pytest.approx(0.5)
    assert mcnemar_exact(0, 0) == 1.0


def test_public_detail_404s_for_analysis_only_event(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from fastapi import HTTPException

    from src.ui.api import public_news

    # Arrange: 生成済み (versions あり) でも、メンバーが分析専用だけなら出さない
    ana = SimpleNamespace(status="posted", duplicate_of=None, analysis_only="geo_rescue")
    repo = SimpleNamespace(
        get_event_item=lambda _id: SimpleNamespace(state=SimpleNamespace(member_ids=("ana",))),
        list_event_versions=lambda _id: [object()],
        get_articles_by_ids=lambda _ids: {"ana": ana},
    )
    monkeypatch.setattr(public_news, "_repo", lambda: repo)
    monkeypatch.setattr(public_news, "_is_public", lambda _r: True)
    monkeypatch.setattr(public_news, "_citations", lambda _repo, _id: [{"article_id": "ana"}])

    # Act / Assert
    with pytest.raises(HTTPException) as exc:
        public_news.get_public_news("x")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_theatre_hint_admitted_items_are_analysis_only_in_analysis_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.pipeline.filters import _filter_by_triage

    # Arrange: triage は low、救済なし、戦域ヒントが 1 (取り込みに効く)
    monkeypatch.setenv("GEO_RESCUE_MODE", "analysis")
    monkeypatch.setenv("INGEST_GEO_THEATRE_HINT", "1")
    monkeypatch.setenv("INGEST_RULE_V2", "0")
    monkeypatch.setenv("CYBER_POLICY_RESCUE", "0")
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "0")
    llm: Any = AsyncMock()
    llm.generate_structured.return_value = TriageDecision(importance="low", reason="地政学")
    out: set[str] = set()

    # Act
    survivors, *_ = await _filter_by_triage(
        [_article()], llm, keep_importance={"medium", "high"}, max_keep=5, analysis_only_out=out
    )

    # Assert
    assert [a.id for a in survivors] == ["a"]
    assert out == {"a"}
