"""victim_country_gate のテスト (2026-10-10)。

監査 (data/eval/victim_country_audit_v1.jsonl) で判明した「非インシデント記事の
被害国誤付与」を落とす関門の単体テスト。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.cti.victim_country_gate import (
    GATE_ENV,
    NON_INCIDENT_ARTICLE_TYPES,
    should_drop_victim_country,
)
from src.pipeline.briefing import _build_briefing
from src.pipeline.summary import SummaryOutput
from src.tools.article_model import Article


class TestNonIncidentTypesWithoutOrgs:
    @pytest.mark.parametrize("article_type", sorted(NON_INCIDENT_ARTICLE_TYPES))
    def test_drops_when_no_victim_org_named(self, article_type: str) -> None:
        # Arrange
        victim_orgs: list[str] = []

        # Act
        result = should_drop_victim_country(article_type, victim_orgs)

        # Assert
        assert result is True

    @pytest.mark.parametrize("article_type", sorted(NON_INCIDENT_ARTICLE_TYPES))
    def test_keeps_when_victim_org_named(self, article_type: str) -> None:
        # Arrange
        victim_orgs = ["Acme Corp"]

        # Act
        result = should_drop_victim_country(article_type, victim_orgs)

        # Assert
        assert result is False

    @pytest.mark.parametrize("article_type", sorted(NON_INCIDENT_ARTICLE_TYPES))
    def test_whitespace_only_org_counts_as_none(self, article_type: str) -> None:
        # Arrange
        victim_orgs = ["   ", ""]

        # Act
        result = should_drop_victim_country(article_type, victim_orgs)

        # Assert
        assert result is True


class TestIncidentTypesAlwaysKeep:
    @pytest.mark.parametrize("article_type", ["breaking", "recap"])
    def test_keeps_regardless_of_victim_orgs(self, article_type: str) -> None:
        # Arrange
        victim_orgs: list[str] = []

        # Act
        result = should_drop_victim_country(article_type, victim_orgs)

        # Assert
        assert result is False


class TestEnvFlagRollback:
    def test_flag_zero_disables_gate_even_without_org(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv(GATE_ENV, "0")

        # Act
        result = should_drop_victim_country("advisory", [])

        # Assert
        assert result is False

    def test_flag_unset_defaults_to_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.delenv(GATE_ENV, raising=False)

        # Act
        result = should_drop_victim_country("press", [])

        # Assert
        assert result is True


def _article() -> Article:
    return Article(
        id="rss:abc",
        title="t",
        url="https://example.com/a",
        summary_html="",
        published=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
        feed_title="f",
        feed_url="https://example.com/",
    )


def _summary(**kw: object) -> SummaryOutput:
    base: dict[str, object] = {
        "title_ja": "見出し",
        "importance": "medium",
        "category": "advisory",
        "summary": "正常な要約です。",
    }
    return SummaryOutput(**{**base, **kw})  # type: ignore[arg-type]


class TestBriefingWiring:
    """src.pipeline.briefing._build_briefing の正規化 seam が関門を実際に通すこと。"""

    def test_press_article_without_victim_org_drops_country_metadata(self) -> None:
        # Arrange
        summary = _summary(article_type="press", victim_country="United States", victim_orgs=[])

        # Act
        msg = _build_briefing(_article(), summary)

        # Assert
        assert "victim_country_iso" not in msg.metadata
        assert "victim_country_raw" not in msg.metadata

    def test_breaking_article_without_victim_org_keeps_country_metadata(self) -> None:
        # Arrange
        summary = _summary(article_type="breaking", victim_country="United States", victim_orgs=[])

        # Act
        msg = _build_briefing(_article(), summary)

        # Assert
        assert msg.metadata.get("victim_country_iso") == "US"
