"""参照先が投稿の主題に触れているかの関門 (grok/link_relevance) のテスト。

実測 (44 投稿) の悪化の型 = 情報の無い参照先 (被害組織のトップページ・ダミーテキスト・
トラッカーの概要ページ) を再現する。組織名・ドメインは架空 (example)。
"""

from __future__ import annotations

import json

import pytest

from src.grok.link_fetch import LinkedPage, build_link_section, linked_context_json
from src.grok.link_relevance import (
    SKIP_OFF_TOPIC,
    apply_relevance_gate,
    is_on_topic,
    link_relevance_gate_enabled,
)

_ALERT = (
    "📢 Ransomware Alert: 🇺🇸\n\n"
    "Acme Credit Union (https://acme-cu.example a US-based banking organization, "
    "has reportedly fallen victim to Zeta ransomware.\n\n"
    "🔍 Key Details:\n🛡️ Threat Actor: Zeta\n📅 Reported on: 16/10/2026"
)
_HOMEPAGE = (
    "Acme Credit Union Success takes direction. At Acme Credit Union we believe in helping "
    "our members choose the best path. Get preapproved. Drive your dream car. " * 3
)


def _page(text: str, url: str = "https://acme-cu.example/") -> LinkedPage:
    return LinkedPage(url=url, final_url=url, ok=True, text=text)


# ---------- 主題に触れない参照先を弾く ----------


def test_victim_homepage_that_only_shares_the_organization_name_is_off_topic() -> None:
    assert is_on_topic(_ALERT, _page(_HOMEPAGE)) is False


def test_dummy_text_page_is_off_topic() -> None:
    post = "‼️Harbor Freight falls victim to Storm Ransomware\nSector: Maritime"
    lorem = "Vestibulum ante ipsum primis in faucibus orci luctus et ultrices posuere. " * 5

    assert is_on_topic(post, _page(lorem)) is False


def test_tracker_overview_that_only_shares_a_month_is_off_topic() -> None:
    post = "🚨 Green Clinic was added to the leak site of the Wallstreet gang. 2 TB stolen."
    tracker = "Ransomware Attack Tracker. Covering January 2018 to October 2026. 33 incidents."

    assert is_on_topic(post, _page(tracker, "https://tracker.example/")) is False


def test_article_that_shares_the_group_name_is_on_topic() -> None:
    article = "Zeta ransomware claimed the attack on Acme Credit Union and leaked data. " * 3

    assert is_on_topic(_ALERT, _page(article, "https://news.example/zeta")) is True


def test_blog_sharing_a_camel_case_product_name_is_on_topic() -> None:
    post = "The Great VM Escape: ESXi Exploitation in the Wild www.vendor.example/blog/esxi"
    blog = "In December we observed an intrusion deploying ESXi exploits from a guest VM."

    assert is_on_topic(post, _page(blog, "https://vendor.example/blog/esxi")) is True


def test_shared_cve_id_is_on_topic() -> None:
    post = "PoC published for CVE-2026-11111 in the Foo gateway"
    advisory = "The vendor advisory covers CVE-2026-11111 and the fixed versions."

    assert is_on_topic(post, _page(advisory, "https://vendor.example/a")) is True


def test_a_single_plain_capitalized_word_is_not_enough() -> None:
    post = (
        "Acme Health, Inc. (https://acme.example a Hospitals and Health Care provider, hit by Zeta"
    )
    page = "We deliver Care across the full spectrum of services for patients."

    assert is_on_topic(post, _page(page, "https://acme.example/")) is False


def test_two_plain_capitalized_words_are_enough() -> None:
    post = "Operation Moonlight targets Parliament Services"
    page = "The Moonlight campaign abused Parliament Services accounts."

    assert is_on_topic(post, _page(page, "https://blog.example/m")) is True


def test_japanese_post_without_proper_terms_cannot_be_judged_and_passes() -> None:
    post = "サイバーセキュリティ戦略本部が新しい方針を公表した。"

    assert is_on_topic(post, _page("方針の全文。" * 50, "https://gov.example/x")) is True


def test_katakana_name_shared_with_page_is_on_topic() -> None:
    post = "ビジネステック社が不正アクセスの被害を公表"
    page = "株式会社ビジネステックは、当社サーバーへの不正アクセスを確認しました。"

    assert is_on_topic(post, _page(page, "https://bt.example/news")) is True


def test_name_derived_from_the_page_host_does_not_count() -> None:
    post = "Globex fell victim to Zeta ransomware (Globex Holdings)"
    page = "Welcome to Globex. Globex builds things."

    assert is_on_topic(post, _page(page, "https://www.globex.example/")) is False


def test_single_plain_word_is_enough_on_a_site_that_is_not_the_named_organization() -> None:
    post = "The French supermarket cyberattacks highlight the risks of unmanaged devices"
    blog = "French retailers were hit by a wave of attacks on store systems. " * 3

    assert is_on_topic(post, _page(blog, "https://blog.example/retail")) is True


def test_cve_id_only_in_the_url_path_counts() -> None:
    post = "Pre-auth RCE in Foo forum software CVE-2026-22222 exploit published"
    stub = "Repository for research. See the README for details and build steps. " * 3

    assert is_on_topic(post, _page(stub, "https://code.example/user/CVE-2026-22222")) is True


def test_location_adjective_is_not_an_event_term() -> None:
    post = "Acme Credit Union (https://acme-cu.example a US-based organization fell victim"

    assert is_on_topic(post, _page("US-based members welcome. " * 20)) is True  # 語が無く判定しない


def test_japanese_post_with_few_terms_on_another_site_is_not_judged() -> None:
    post = "【注意喚起】偽アプリの導入により端末を遠隔操作され、送金被害が発生。ABCセンター"

    assert is_on_topic(post, _page("対処方法の解説。" * 30, "https://other.example/a")) is True


# ---------- 適用 ----------


def test_gate_marks_off_topic_page_but_keeps_text_and_ok() -> None:
    pages = (_page(_HOMEPAGE),)

    gated = apply_relevance_gate(_ALERT, pages)

    assert gated[0].skip_reason == SKIP_OFF_TOPIC
    assert gated[0].ok is True
    assert gated[0].text == _HOMEPAGE
    assert pages[0].skip_reason is None  # 元は変えない (immutable)


def test_gate_keeps_on_topic_and_failed_pages_untouched() -> None:
    good = _page(
        "Zeta ransomware claimed the attack on Acme Credit Union. " * 10, "https://news.example/z"
    )
    failed = LinkedPage(url="https://x.example", final_url=None, ok=False, reason="timeout")

    gated = apply_relevance_gate(_ALERT, (good, failed))

    assert gated == (good, failed)


def test_section_excludes_skipped_pages_and_context_records_the_reason() -> None:
    gated = apply_relevance_gate(_ALERT, (_page(_HOMEPAGE),))

    assert build_link_section(gated) == ""
    stored = json.loads(linked_context_json(gated) or "[]")
    assert stored[0]["skip_reason"] == SKIP_OFF_TOPIC
    assert stored[0]["text"]


def test_section_uses_only_the_relevant_page_when_mixed() -> None:
    good = _page(
        "Zeta ransomware claimed the attack on Acme Credit Union. " * 10, "https://news.example/z"
    )
    gated = apply_relevance_gate(_ALERT, (_page(_HOMEPAGE), good))

    section = build_link_section(gated)

    assert "news.example/z" in section
    assert "acme-cu.example" not in section


# ---------- flag ----------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("1", True), ("0", False), ("off", False), ("garbage", True)],
)
def test_gate_flag_defaults_on(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    if value is None:
        monkeypatch.delenv("GROK_LINK_RELEVANCE_GATE", raising=False)
    else:
        monkeypatch.setenv("GROK_LINK_RELEVANCE_GATE", value)

    assert link_relevance_gate_enabled() is expected
