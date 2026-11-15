"""被害組織自身の正規 host を IOC にしない関門 (2026-11-15)。"""

from __future__ import annotations

import pytest

from src.cti.ioc_victim_filter import (
    has_hosting_cue,
    is_victim_own_host,
    registrable_label,
    victim_filter_mode,
)


def test_registrable_label_handles_second_level_cctld() -> None:
    assert registrable_label("shop.acme.co.jp") == "acme"
    assert registrable_label("www.acme.com") == "acme"
    assert registrable_label("localhost") == ""


def test_drops_victim_site_by_org_name_with_legal_suffix() -> None:
    assert is_victim_own_host("leo-pharma.com", victim_orgs=["LEO Pharma A/S"])
    assert is_victim_own_host("https://www.acme.co.jp/login", victim_orgs=["株式会社acme"])


def test_drops_leak_listing_when_org_is_the_domain() -> None:
    assert is_victim_own_host("https://roundglass.com", victim_orgs=["roundglass[.]com"])


def test_keeps_typosquat_when_org_is_a_domain() -> None:
    # TLD 違いは攻撃者の登録の可能性がある
    assert is_victim_own_host("america250.holiday", victim_orgs=["america250.org"]) is None


def test_keeps_unrelated_host() -> None:
    assert is_victim_own_host("evil-c2.example", victim_orgs=["Acme Corp"]) is None
    assert is_victim_own_host("evil.example", victim_orgs=[]) is None


def test_short_label_is_not_matched() -> None:
    assert is_victim_own_host("abc.com", victim_orgs=["ABC"]) is None


def test_keeps_host_when_context_shows_malware_hosting() -> None:
    ctx = ("Watering hole attack on Acme",)
    assert is_victim_own_host("acme.com", victim_orgs=["Acme"], context_texts=ctx) is None
    assert has_hosting_cue("supply chain compromise")
    assert not has_hosting_cue("ransomware leak post", None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "shadow"), ("0", "off"), ("1", "on"), ("shadow", "shadow")],
)
def test_mode(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: str) -> None:
    if raw is None:
        monkeypatch.delenv("IOC_VICTIM_HOST_FILTER", raising=False)
    else:
        monkeypatch.setenv("IOC_VICTIM_HOST_FILTER", raw)
    assert victim_filter_mode() == expected
