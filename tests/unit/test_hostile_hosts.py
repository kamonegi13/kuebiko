"""敵対的 host の分類器 (純粋) のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools.hostile_hosts import (
    REASON_CCTLD,
    REASON_DOMAIN,
    REASON_GEOIP,
    REASON_SOURCE_TAG,
    REASON_UNPARSEABLE,
    HostileLists,
    classify_host,
    load_lists,
    normalize_host,
)

LISTS = HostileLists(
    cctlds=frozenset({"cn", "ru", "xn--p1ai"}),
    domains=frozenset({"vendor.kuebiko.example", "sub.state.kuebiko.example"}),
)


class TestClassifyHost:
    def test_cctld_match_is_hostile(self) -> None:
        # Arrange / Act
        v = classify_host("https://www.example.cn/news", lists=LISTS)
        # Assert
        assert v.hostile
        assert v.reasons == (REASON_CCTLD,)

    def test_unicode_rf_tld_is_hostile_via_punycode(self) -> None:
        v = classify_host("https://пример.рф/", lists=LISTS)
        assert v.hostile
        assert REASON_CCTLD in v.reasons

    def test_registered_domain_suffix_match(self) -> None:
        v = classify_host("https://blog.vendor.kuebiko.example/x", lists=LISTS)
        assert v.hostile
        assert v.reasons == (REASON_DOMAIN,)

    def test_domain_match_is_not_substring(self) -> None:
        assert not classify_host("https://notvendor.kuebiko.example/", lists=LISTS).hostile

    def test_ordinary_host_is_not_hostile(self) -> None:
        v = classify_host("https://www.cisa.gov/feed", lists=LISTS)
        assert not v.hostile
        assert v.reasons == ()

    def test_source_tag_forces_hostile_and_accumulates_reasons(self) -> None:
        v = classify_host("https://x.example.ru/", source_profile="hostile", lists=LISTS)
        assert v.hostile
        assert set(v.reasons) == {REASON_SOURCE_TAG, REASON_CCTLD}

    def test_default_profile_does_not_mark_hostile(self) -> None:
        v = classify_host("https://x.example.org/", source_profile="default", lists=LISTS)
        assert not v.hostile

    @pytest.mark.parametrize("bad", ["", "   ", "http://", "https://[::1"])
    def test_unparseable_host_fails_toward_hostile(self, bad: str) -> None:
        v = classify_host(bad, lists=LISTS)
        assert v.hostile
        assert REASON_UNPARSEABLE in v.reasons

    def test_geoip_countries_make_hostile_only_when_given(self) -> None:
        assert not classify_host("https://a.example.com/", lists=LISTS).hostile
        v = classify_host("https://a.example.com/", ip_countries=frozenset({"RU"}), lists=LISTS)
        assert v.hostile
        assert v.reasons == (REASON_GEOIP,)
        v2 = classify_host("https://a.example.com/", ip_countries=frozenset({"US"}), lists=LISTS)
        assert not v2.hostile


def test_normalize_host_lowercases_and_strips_port() -> None:
    assert normalize_host("HTTPS://News.Example.CN:8443/a") == "news.example.cn"


class TestShippedConfig:
    def test_shipped_generic_config_has_cctlds_and_no_domains(self) -> None:
        # 公開版は ccTLD の規則だけ。実際の登録ドメインはローカル層 (config/local/) にある
        lists = load_lists()
        assert {"cn", "ru", "su", "xn--p1ai", "by", "kp", "ir"} <= lists.cctlds
        assert lists.domains == frozenset()

    def test_missing_config_falls_back_to_builtin_cctlds(self, tmp_path: Path) -> None:
        lists = load_lists(tmp_path / "nope.yaml")
        assert "ru" in lists.cctlds
        assert lists.domains == frozenset()
