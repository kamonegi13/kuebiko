"""出典ドメインが IOC として保存されるのを遮断する (2026-08-19)。

判定基準は「出典系の URL は **IOC ではない**。含めないこと」と明記しているのに、実測で
ioc_domain/ioc_url 3,403 件中 44 件が購読ソースのドメイン (twz.com 20 等)、32 件が
その記事自身の出典ホストだった。⭐ **指示では止まらない** — 決定論の関門で遮断する。
"""

from __future__ import annotations

from typing import Any

from src.cti.ioc_source_filter import _hosts_from_entries, is_source_reference, normalize_host

_KNOWN = frozenset({"twz.com", "justsecurity.org", "wiz.io"})


class TestNormalizeHost:
    def test_extracts_host_from_url_and_bare_domain(self) -> None:
        assert normalize_host("https://evil.example/path?a=1") == "evil.example"
        assert normalize_host("evil.example") == "evil.example"
        assert normalize_host("evil.example/path") == "evil.example"

    def test_strips_www_case_and_trailing_dot(self) -> None:
        # 同じ host が別扱いになると filter が素通りする
        assert normalize_host("WWW.TWZ.COM.") == "twz.com"
        assert normalize_host("https://WWW.Twz.com/a") == "twz.com"

    def test_empty_input_is_empty(self) -> None:
        assert normalize_host("   ") == ""


class TestIsSourceReference:
    def test_article_own_host_is_a_reference(self) -> None:
        # Arrange/Act/Assert: 購読一覧に無くても自記事の出典なら弾く
        assert is_source_reference(
            "example-news.test", article_url="https://example-news.test/a/b", hosts=frozenset()
        )

    def test_subscribed_source_host_is_a_reference(self) -> None:
        assert is_source_reference("https://twz.com/story/x", article_url=None, hosts=_KNOWN)

    def test_unrelated_malicious_domain_is_kept(self) -> None:
        # 本物の IOC を落とさないこと (この filter の唯一の危険)
        assert not is_source_reference(
            "attacker.example", article_url="https://twz.com/story/x", hosts=_KNOWN
        )

    def test_subdomain_is_treated_as_a_different_host(self) -> None:
        # 保守的な選択: 部分一致にすると C2 が乗った正規サブドメインを落としかねない
        assert not is_source_reference("cdn.twz.com", article_url=None, hosts=_KNOWN)

    def test_empty_value_is_not_a_reference(self) -> None:
        assert not is_source_reference("", article_url="https://twz.com/a", hosts=_KNOWN)


class TestHostsFromEntries:
    def test_collects_urls_regardless_of_key_name(self) -> None:
        # transport ごとに key 名が違う (url / sitemap / site) ため key を列挙しない
        entries: list[dict[str, Any]] = [
            {"url": "https://feeds.example/rss.xml"},
            {"sitemap": "https://watch.example/sitemap.xml", "enabled": True},
            {"site": "http://www.Scrape.example/news", "folder": "jp"},
        ]
        assert _hosts_from_entries(entries) == {"feeds.example", "watch.example", "scrape.example"}

    def test_entries_without_urls_yield_nothing(self) -> None:
        assert _hosts_from_entries([{"title": "no url here"}]) == set()
