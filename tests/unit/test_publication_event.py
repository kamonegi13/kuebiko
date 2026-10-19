"""発表そのものが出来事の種別は basis=reported を disclosed に読み替える。"""

from __future__ import annotations

import pytest

from src.cti.publication_event import (
    publication_kind,
    resolve_publication_basis,
)


def test_advisory_from_issuer_is_publication() -> None:
    kind = publication_kind(
        article_type="advisory", feed_title="CISA Cybersecurity Advisories", feed_url=""
    )
    assert kind == "advisory"
    assert publication_kind(article_type="advisory", account_class="vendor_official") == "advisory"


def test_advisory_reported_by_news_outlet_is_not_publication() -> None:
    # 報道媒体が勧告・脆弱性を報じた記事 (境界 5 件の型)
    for outlet in ("Example Security News", "Example Daily Wire"):
        assert publication_kind(article_type="advisory", feed_title=outlet) is None


def test_leak_site_by_feed_title() -> None:
    assert publication_kind(feed_title="ransomware.live", category="breach") == "leak_site"
    assert publication_kind(feed_title="Ransomware.live") == "leak_site"


def test_apt_leak_category_alone_is_not_leak_site() -> None:
    # 内部リーク報道の研究記事 (誤り型)
    assert publication_kind(category="apt_leak", article_type="research") is None
    assert publication_kind(category="apt_leak", feed_title="Example Loop") is None


def test_exclusions_apply_to_all_kinds() -> None:
    for t in ("research", "opinion", "tutorial", "recap"):
        assert publication_kind(article_type=t, feed_title="ransomware.live") is None
        assert publication_kind(article_type=t, account_class="gov_official") is None


def test_lessons_and_explainers_from_news_are_not_publication() -> None:
    # 侵害の教訓・マルウェア解説・ベンダー研究 (誤り型)
    assert publication_kind(article_type="advisory", feed_title="Example Blog") is None
    assert publication_kind(article_type="research", feed_title="Example Intel") is None


def test_authority_by_gov_account_class() -> None:
    assert publication_kind(article_type="press", account_class="gov_official") == "authority"


def test_authority_not_for_vendor_or_opinion() -> None:
    assert publication_kind(article_type="breaking", account_class="vendor_official") is None
    assert publication_kind(article_type="opinion", account_class="gov_official") is None


def test_plain_news_is_not_publication() -> None:
    assert publication_kind(article_type="breaking", category="breach", feed_title="X News") is None


def test_reported_becomes_disclosed_for_publication() -> None:
    got = resolve_publication_basis(
        "reported", article_type="advisory", account_class="gov_official"
    )
    assert got == "disclosed"


def test_other_basis_untouched() -> None:
    assert resolve_publication_basis("occurred", article_type="advisory") == "occurred"
    assert resolve_publication_basis(None, article_type="advisory") is None


def test_non_publication_reported_untouched() -> None:
    assert resolve_publication_basis("reported", article_type="breaking") == "reported"


def test_flag_off_restores_old_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENT_DATE_PUBLICATION_AS_EVENT", "0")
    got = resolve_publication_basis(
        "reported", article_type="advisory", account_class="gov_official"
    )
    assert got == "reported"


def test_advisory_on_x_requires_gov_official() -> None:
    # X の投稿は gov_official 以外 (vendor_official・無印) では advisory にしない
    for acls in ("vendor_official", "analyst_known", ""):
        assert (
            publication_kind(
                article_type="advisory",
                feed_title="@ExampleVendor",
                feed_url="https://x.com/ExampleVendor",
                account_class=acls,
            )
            is None
        )
    got = publication_kind(
        article_type="advisory",
        feed_title="@ExampleAgency",
        feed_url="https://twitter.com/ExampleAgency",
        account_class="gov_official",
    )
    assert got == "advisory"
