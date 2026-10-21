"""_build_briefing が account_class を発表そのものの判定へ渡すことの回帰テスト。

X の勧告は gov_official のときだけ disclosed へ転換される (publication_event)。
account_class が _build_briefing から届かず reported のままだった不具合を固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.pipeline.briefing import _build_briefing
from src.pipeline.summary import SummaryOutput
from src.tools.article_model import Article


def _x_article(account_class: str) -> Article:
    return Article(
        id="x:1",
        title="t",
        url="https://x.com/ExampleAgency/status/1",
        summary_html="",
        published=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
        feed_title="@ExampleAgency",
        feed_url="https://x.com/ExampleAgency",
        account_class=account_class,
    )


def _advisory_summary() -> SummaryOutput:
    return SummaryOutput(
        title_ja="見出し",
        importance="medium",
        category="advisory",
        summary="正常な要約です。",
        article_type="advisory",
        event_date="2026-10-09",
        event_date_basis="reported",
    )


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVENT_DATE_PUBLICATION_AS_EVENT", raising=False)


def test_x_gov_official_advisory_becomes_disclosed() -> None:
    # Arrange
    article = _x_article("gov_official")

    # Act
    msg = _build_briefing(article, _advisory_summary())

    # Assert
    assert msg.metadata.get("event_date_basis") == "disclosed"


def test_x_vendor_official_advisory_stays_reported() -> None:
    # Arrange
    article = _x_article("vendor_official")

    # Act
    msg = _build_briefing(article, _advisory_summary())

    # Assert
    assert msg.metadata.get("event_date_basis") == "reported"
