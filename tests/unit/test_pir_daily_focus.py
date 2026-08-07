"""Unit tests for src.digest.pir_daily_focus."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.digest.pir_daily_focus import (
    PirFocusSection,
    _filter_meaningful_matches,
    _format_match_line,
    _format_section,
    _sort_matches,
    collect_pir_focus_sections,
)
from src.pir.evaluator import PirMatch
from src.pir.models import Pir, PirConfig, SpotlightConfig, StrongSignals


def _make_match(
    article_id: str = "a1",
    importance: str = "medium",
    title: str = "Title",
    feed_title: str = "Feed",
    url: str = "https://example.com/a1",
    created_at: str = "2026-05-25T05:00:00+00:00",
) -> PirMatch:
    return PirMatch(
        article_id=article_id,
        title=title,
        url=url,
        feed_title=feed_title,
        importance=importance,
        posted_channel="brief",
        created_at=created_at,
        matched_via=("keywords",),
    )


def _make_pir(pir_id: str = "pir_test", title: str = "テスト PIR") -> Pir:
    return Pir(
        id=pir_id,
        title=title,
        description="description",
        enabled=True,
        strong_signals=StrongSignals(keywords=["test"]),
        spotlight=SpotlightConfig(enabled=False),
    )


def test_filter_meaningful_drops_low() -> None:
    matches = [
        _make_match(article_id="a1", importance="high"),
        _make_match(article_id="a2", importance="medium"),
        _make_match(article_id="a3", importance="low"),
        _make_match(article_id="a4", importance=""),
    ]
    filtered = _filter_meaningful_matches(matches)
    assert [m.article_id for m in filtered] == ["a1", "a2"]


def test_sort_matches_importance_then_recency() -> None:
    matches = [
        _make_match(
            article_id="med_old",
            importance="medium",
            created_at="2026-05-25T00:00:00+00:00",
        ),
        _make_match(
            article_id="high_old",
            importance="high",
            created_at="2026-05-24T00:00:00+00:00",
        ),
        _make_match(
            article_id="med_new",
            importance="medium",
            created_at="2026-05-25T03:00:00+00:00",
        ),
    ]
    sorted_ = _sort_matches(matches)
    assert [m.article_id for m in sorted_] == ["high_old", "med_new", "med_old"]


def test_format_match_line_contains_markdown_link() -> None:
    m = _make_match(
        article_id="x", title="Some Title", url="https://example.com/x", importance="high"
    )
    line = _format_match_line(m)
    assert "[Some Title](https://example.com/x)" in line
    assert "[high]" in line
    assert "🔴" in line


def test_format_match_line_source_reliability_marker() -> None:
    """S1: 朝の Discord 通読で SNS/一次の信頼度を一目で (tier は feed メタから決定的)。"""
    grok = PirMatch(
        article_id="g",
        title="T",
        url="https://e/g",
        feed_title="Grok",
        importance="high",
        posted_channel="brief",
        created_at="2026-06-10",
        matched_via=("actors",),
        feed_url="https://grok.com/",
    )
    cisa = PirMatch(
        article_id="c",
        title="T",
        url="https://e/c",
        feed_title="CISA",
        importance="high",
        posted_channel="brief",
        created_at="2026-06-10",
        matched_via=("actors",),
        feed_url="https://www.cisa.gov/a",
    )
    news = _make_match(feed_title="BleepingComputer", url="https://bleepingcomputer.com/x")
    assert "⚠SNS" in _format_match_line(grok)  # Grok = 要裏取り
    assert "✓一次" in _format_match_line(cisa)  # CISA = 一次 advisory
    assert "⚠SNS" not in _format_match_line(news) and "✓一次" not in _format_match_line(news)


def test_format_section_includes_llm_summary_when_present() -> None:
    pir = _make_pir()
    section = PirFocusSection(
        pir=pir,
        matches=[_make_match()],
        total_match_count=1,
        llm_summary="本日の要点 sample",
    )
    out = _format_section(section)
    assert pir.title in out
    assert "1 match" in out
    assert "💡 本日の要点 sample" in out


def test_format_section_omits_summary_when_blank() -> None:
    pir = _make_pir()
    section = PirFocusSection(
        pir=pir,
        matches=[_make_match()],
        total_match_count=1,
        llm_summary="",
    )
    out = _format_section(section)
    assert "💡" not in out


def test_format_section_shows_top_count_when_truncated() -> None:
    pir = _make_pir()
    section = PirFocusSection(
        pir=pir,
        matches=[_make_match(article_id="a1"), _make_match(article_id="a2")],
        total_match_count=10,
        llm_summary="",
    )
    out = _format_section(section)
    assert "10 match, top 2" in out


@pytest.mark.asyncio
async def test_collect_pir_focus_sections_skips_pirs_with_no_meaningful_matches() -> None:
    """match 0 件 / low のみの PIR は section 化されない。"""
    pir_with_matches = _make_pir(pir_id="pir_a", title="A")
    pir_empty = _make_pir(pir_id="pir_b", title="B")
    pir_low_only = _make_pir(pir_id="pir_c", title="C")

    pir_config = PirConfig(priorities=[pir_with_matches, pir_empty, pir_low_only])

    def fake_evaluate(pir: Pir, **_: object) -> list[PirMatch]:
        if pir.id == "pir_a":
            return [_make_match(article_id="a1", importance="high")]
        if pir.id == "pir_b":
            return []
        # pir_c: low only
        return [_make_match(article_id="c1", importance="low")]

    llm_mock = AsyncMock()
    llm_mock.generate = AsyncMock(
        return_value=type("R", (), {"text": "要点 sample"})(),
    )

    with (
        patch("src.digest.pir_daily_focus.load_pir_config", return_value=pir_config),
        patch(
            "src.digest.pir_daily_focus.evaluate_pir_matches",
            side_effect=fake_evaluate,
        ),
    ):
        sections = await collect_pir_focus_sections(llm=llm_mock, lookback_hours=24)

    assert [s.pir.id for s in sections] == ["pir_a"]
    assert sections[0].matches[0].article_id == "a1"
    assert sections[0].llm_summary == "要点 sample"


@pytest.mark.asyncio
async def test_collect_pir_focus_sections_llm_failure_keeps_section() -> None:
    """LLM 例外時も section 自体は残り、llm_summary が空文字になる。"""
    pir = _make_pir(pir_id="pir_a")
    pir_config = PirConfig(priorities=[pir])

    llm_mock = AsyncMock()
    llm_mock.generate = AsyncMock(side_effect=RuntimeError("ollama down"))

    with (
        patch("src.digest.pir_daily_focus.load_pir_config", return_value=pir_config),
        patch(
            "src.digest.pir_daily_focus.evaluate_pir_matches",
            return_value=[_make_match(article_id="a1", importance="high")],
        ),
    ):
        sections = await collect_pir_focus_sections(llm=llm_mock, lookback_hours=24)

    assert len(sections) == 1
    assert sections[0].llm_summary == ""


class TestFormatCompactDigest:
    """Discord 要点射影 (2026-07-12): 1 PIR = 1 行、記事リンクは Web で読む。"""

    def test_one_line_per_pir_without_article_links(self) -> None:
        from src.digest.pir_daily_focus import format_compact_digest

        sections = [
            PirFocusSection(
                pir=_make_pir(pir_id="p1", title="中国 APT 動向"),
                matches=[_make_match(article_id="a1", importance="high")],
                total_match_count=4,
                llm_summary="Volt Typhoon が通信事業者へ接近。",
            ),
            PirFocusSection(
                pir=_make_pir(pir_id="p2", title="日本標的"),
                matches=[_make_match(article_id="a2", importance="medium")],
                total_match_count=1,
                llm_summary="",
            ),
        ]
        text = format_compact_digest(sections, "2026-07-12")
        lines = text.splitlines()
        assert len(lines) == 3  # header + 2 PIR
        assert "2 領域" in lines[0]
        assert lines[1].startswith("🔴 **中国 APT 動向** (4 match)")
        assert "Volt Typhoon" in lines[1]
        assert lines[2].startswith("🟡 **日本標的** (1 match)")
        # full digest と違い記事 URL は載せない
        assert "http" not in text

    def test_long_summary_is_clipped(self) -> None:
        from src.digest.pir_daily_focus import format_compact_digest

        sections = [
            PirFocusSection(
                pir=_make_pir(),
                matches=[_make_match(article_id="a1", importance="low")],
                total_match_count=2,
                llm_summary="あ" * 300,
            )
        ]
        text = format_compact_digest(sections, "2026-07-12")
        pir_line = text.splitlines()[1]
        assert len(pir_line) < 220
        assert pir_line.endswith("…")

    def test_empty_sections_returns_empty(self) -> None:
        from src.digest.pir_daily_focus import format_compact_digest

        assert format_compact_digest([], "2026-07-12") == ""
