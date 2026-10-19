"""発表そのものが出来事の種別は basis=reported を disclosed に読み替える。"""

from __future__ import annotations

import pytest

from src.cti.publication_event import (
    publication_kind,
    resolve_publication_basis,
)


def test_advisory_type_is_publication() -> None:
    assert publication_kind(article_type="advisory", category="vulnerability") == "advisory"


def test_leak_site_by_feed_title() -> None:
    assert publication_kind(feed_title="ransomware.live", category="breach") == "leak_site"
    assert publication_kind(feed_title="Ransomware.live") == "leak_site"


def test_leak_site_by_category() -> None:
    assert publication_kind(category="apt_leak") == "leak_site"


def test_authority_by_gov_account_class() -> None:
    assert publication_kind(article_type="press", account_class="gov_official") == "authority"


def test_authority_not_for_vendor_or_opinion() -> None:
    assert publication_kind(article_type="breaking", account_class="vendor_official") is None
    assert publication_kind(article_type="opinion", account_class="gov_official") is None


def test_plain_news_is_not_publication() -> None:
    assert publication_kind(article_type="breaking", category="breach", feed_title="X News") is None


def test_reported_becomes_disclosed_for_publication() -> None:
    assert resolve_publication_basis("reported", article_type="advisory") == "disclosed"


def test_other_basis_untouched() -> None:
    assert resolve_publication_basis("occurred", article_type="advisory") == "occurred"
    assert resolve_publication_basis(None, article_type="advisory") is None


def test_non_publication_reported_untouched() -> None:
    assert resolve_publication_basis("reported", article_type="breaking") == "reported"


def test_flag_off_restores_old_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENT_DATE_PUBLICATION_AS_EVENT", "0")
    assert resolve_publication_basis("reported", article_type="advisory") == "reported"
