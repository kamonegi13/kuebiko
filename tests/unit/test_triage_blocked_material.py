"""triage の材料から確認・拒否の画面の文を外す (2026-11-20、11-19 監査 D2 P1-8a)。

先行取得した本文や feed の抜粋が Cloudflare の確認画面の文で、LLM が「概要はボット検証画面の
定型文のみ」と書いて low にしていた。旗 TRIAGE_BLOCKED_MATERIAL の 0 / shadow / 1 で、
材料・判定・落選の理由・先行取得がどう変わるかを固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import src.pipeline.filters as filters_mod
from src.pipeline.briefing import _degenerate_body_reason
from src.pipeline.filters import _filter_by_triage
from src.pipeline.triage_material import (
    NO_MATERIAL_PREFIX,
    blocked_fields,
    material_for_triage,
)
from src.storage.repo_triage_rejections import TriageRejectionRow
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.article_triage import TriageDecision
from src.tools.block_page import is_block_page
from src.tools.content_extractor import ExtractionResult

# 本番で取れた画面の文面 (ホスト名は example に置換)
CLOUDFLARE_PAGE = (
    "www.example.org Performing security verification This website uses a security service"
    " to protect against malicious bots. This page is displayed while the website verifies"
    " you are not a bot."
)
CONNECTION_REVIEW_PAGE = (
    "www.example.com needs to review the security of your connection before proceeding."
)
REAL_SUMMARY = "<p>Analysts assess the regional security implications of the new agreement.</p>"


def _article(
    aid: str = "a1",
    *,
    title: str = "Regional security and the new agreement",
    summary_html: str = REAL_SUMMARY,
    body_text: str | None = None,
) -> Article:
    return Article(
        id=aid,
        title=title,
        url=f"https://example.com/{aid}",
        summary_html=summary_html,
        published=datetime(2026, 11, 20, tzinfo=UTC),
        feed_title="Feed",
        feed_url="https://example.com/feed",
        body_text=body_text,
    )


class TestBlockPage:
    @pytest.mark.parametrize("text", [CLOUDFLARE_PAGE, CONNECTION_REVIEW_PAGE])
    def test_detects_real_challenge_pages(self, text: str) -> None:
        assert is_block_page(text)

    def test_long_article_about_cloudflare_is_not_a_block_page(self) -> None:
        body = CLOUDFLARE_PAGE + " " + "The outage analysis continues with details. " * 60

        assert len(body) > 2000
        assert not is_block_page(body)

    def test_body_gate_still_reports_block_page(self) -> None:
        assert _degenerate_body_reason(CLOUDFLARE_PAGE) == "block_page"


class TestMaterialForTriage:
    def test_off_returns_the_same_article(self) -> None:
        art = _article(body_text=CLOUDFLARE_PAGE)

        assert material_for_triage(art, "off") == (art, False)

    def test_shadow_keeps_material(self) -> None:
        art = _article(body_text=CLOUDFLARE_PAGE)

        out, empty = material_for_triage(art, "shadow")

        assert out is art and empty is False

    def test_on_drops_blocked_body_but_keeps_real_summary(self) -> None:
        art = _article(body_text=CLOUDFLARE_PAGE)

        out, empty = material_for_triage(art, "on")

        assert out.body_text is None
        assert out.summary_html == REAL_SUMMARY
        assert empty is False

    def test_on_with_both_blocked_leaves_title_only(self) -> None:
        art = _article(summary_html=f"<p>{CLOUDFLARE_PAGE}</p>", body_text=CLOUDFLARE_PAGE)

        out, empty = material_for_triage(art, "on")

        assert blocked_fields(art) == ("body_text", "summary_html")
        assert (out.body_text, out.summary_html, empty) == (None, "", True)


async def _run(arts: list[Article]) -> tuple[list[str], list[TriageRejectionRow]]:
    prompts: list[str] = []

    async def _gen(prompt: str, *a: object, **k: object) -> TriageDecision:
        prompts.append(prompt)
        return TriageDecision(importance="low", reason="地政学の論評")

    llm = AsyncMock()
    llm.generate_structured = AsyncMock(side_effect=_gen)
    _s, _n, _ids, _err, rejected, _shadow = await _filter_by_triage(
        arts, llm, keep_importance={"high", "medium"}, max_keep=10
    )
    return prompts, rejected


class TestFilterByTriage:
    @pytest.mark.asyncio
    async def test_flag_off_passes_block_text_as_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRIAGE_BLOCKED_MATERIAL", raising=False)
        monkeypatch.setenv("TRIAGE_GEO_RESCUE", "0")

        prompts, rejected = await _run([_article(summary_html=CLOUDFLARE_PAGE)])

        assert "malicious bots" in prompts[0]
        assert rejected[0].reason == "地政学の論評"

    @pytest.mark.asyncio
    async def test_on_judges_title_only_and_marks_the_rejection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRIAGE_BLOCKED_MATERIAL", "1")
        monkeypatch.setenv("TRIAGE_GEO_RESCUE", "0")

        prompts, rejected = await _run([_article(summary_html=CLOUDFLARE_PAGE)])

        assert "malicious bots" not in prompts[0]
        assert "Regional security and the new agreement" in prompts[0]
        assert rejected[0].reason == NO_MATERIAL_PREFIX + "地政学の論評"

    @pytest.mark.asyncio
    async def test_on_with_real_summary_is_not_marked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRIAGE_BLOCKED_MATERIAL", "1")
        monkeypatch.setenv("TRIAGE_GEO_RESCUE", "0")

        prompts, rejected = await _run([_article(body_text=CLOUDFLARE_PAGE)])

        assert "malicious bots" not in prompts[0]
        assert "regional security implications" in prompts[0]
        assert rejected[0].reason == "地政学の論評"


class _FakeExtractor:
    def __init__(self, text: str) -> None:
        self._text = text

    async def __aenter__(self) -> _FakeExtractor:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def extract(self, url: str) -> ExtractionResult:
        return ExtractionResult(url=url, text=self._text, success=True)


class TestPrefetch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("flag", "kept"), [("shadow", True), ("1", False)])
    async def test_block_page_body_is_dropped_only_when_on(
        self, monkeypatch: pytest.MonkeyPatch, flag: str, kept: bool
    ) -> None:
        monkeypatch.setenv("TRIAGE_BLOCKED_MATERIAL", flag)
        monkeypatch.setattr(
            filters_mod, "ContentExtractor", lambda: _FakeExtractor(CLOUDFLARE_PAGE)
        )

        enriched, count = await filters_mod._prefetch_thin_bodies(
            [_article(summary_html="short")], min_chars=600
        )

        assert (enriched[0].body_text is not None) is kept
        assert count == (1 if kept else 0)


def test_no_material_rejections_are_counted_per_feed(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    rows = [
        TriageRejectionRow("a1", "https://e/1", "t", "F", "https://e/feed", "low", "x"),
        TriageRejectionRow(
            "a2", "https://e/2", "t", "F", "https://e/feed", "low", NO_MATERIAL_PREFIX + "y"
        ),
    ]
    repo.record_triage_rejections(rows)

    total = repo.count_triage_rejections_by_feed(days=7)
    no_material = repo.count_triage_rejections_by_feed(days=7, reason_prefix=NO_MATERIAL_PREFIX)

    assert total == {"https://e/feed": 2}
    assert no_material == {"https://e/feed": 1}
