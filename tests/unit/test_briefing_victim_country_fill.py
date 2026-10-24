"""_build_briefing が被害国の補完 (VICTIM_COUNTRY_FILL) を配線していることのテスト。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.pipeline.briefing import _build_briefing
from src.pipeline.summary import SummaryOutput
from src.tools.article_model import Article


def _article() -> Article:
    return Article(
        id="rss:1",
        title="架空商事株式会社に不正アクセス",
        url="https://news.example/1",
        summary_html="",
        published=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
        feed_title="Example News",
        feed_url="https://news.example/feed",
    )


def _summary(**overrides: object) -> SummaryOutput:
    base: dict[str, object] = {
        "title_ja": "架空商事株式会社に不正アクセス",
        "importance": "medium",
        "category": "breach",
        "summary": "架空商事株式会社は自社サーバへの不正アクセスを公表した。",
        "article_type": "breaking",
        "victim_orgs": ["架空商事株式会社"],
        "event_date": "2026-10-09",
        "event_date_basis": "reported",
    }
    base.update(overrides)
    return SummaryOutput(**base)  # type: ignore[arg-type]


def test_on_mode_fills_country_and_records_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VICTIM_COUNTRY_FILL", "on")

    msg = _build_briefing(_article(), _summary(), body_text="本文")

    assert msg.metadata["victim_country_iso"] == "JP"
    assert msg.metadata["victim_country_raw"] == "inferred:jp_legal_form"


def test_shadow_mode_does_not_change_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VICTIM_COUNTRY_FILL", "shadow")

    msg = _build_briefing(_article(), _summary(), body_text="本文")

    assert "victim_country_iso" not in msg.metadata


def test_off_mode_does_not_change_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VICTIM_COUNTRY_FILL", "0")

    msg = _build_briefing(_article(), _summary(), body_text="本文")

    assert "victim_country_iso" not in msg.metadata


def test_value_from_llm_is_never_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VICTIM_COUNTRY_FILL", "on")

    msg = _build_briefing(_article(), _summary(victim_country="Taiwan"), body_text="本文")

    assert msg.metadata["victim_country_iso"] == "TW"


def test_non_breaking_article_is_not_filled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VICTIM_COUNTRY_FILL", "on")

    msg = _build_briefing(_article(), _summary(article_type="research"), body_text="本文")

    assert "victim_country_iso" not in msg.metadata
