"""victim_org のベンダ混入遮断 (2026-08-19)。

判定基準は「**含めないもの**: ベンダ / ソフトメーカー」と明記しているのに、30 日実測で
Google 19 / Microsoft 17 / Cisco 6 等 49 件が混入し、地図の組織本社プロットと
被害国 KPI に偽の点を出していた (バグバウンティの報奨金記事・製品対応記事)。
⭐ **指示では止まらない** — 決定論の関門で遮断する。
"""

from __future__ import annotations

from src.cti.victim_org_filter import PROTECTED_CATEGORIES, is_vendor_noise


class TestVendorNoise:
    def test_vendors_are_blocked_outside_breach_categories(self) -> None:
        """実測で混入していた提供元を弾く。"""
        for org in ("Google", "Microsoft", "Cisco", "Apple", "Oracle", "OpenAI"):
            assert is_vendor_noise(org, "research"), org

    def test_case_and_whitespace_insensitive(self) -> None:
        assert is_vendor_noise("  microsoft  ", "vulnerability")
        assert is_vendor_noise("HUGGING FACE", "malware")

    def test_real_victims_are_kept(self) -> None:
        """被害組織は弾かない (地図と KPI の原料を殺さない)。"""
        for org in ("Toyota", "日本交通", "AT&T", "Ticketmaster", "ニチレイ"):
            assert not is_vendor_noise(org, "breach"), org
            assert not is_vendor_noise(org, "research"), org


class TestBreachedVendorIsProtected:
    """⚠ **そのベンダ自身が侵害された記事は残す**。

    「OpenAI 社内 Slack 侵害」のような実被害を巻き込まないための保守則
    (2026-08-01 の掃除 script が確立した規約)。
    """

    def test_vendor_in_breach_category_survives(self) -> None:
        for category in sorted(PROTECTED_CATEGORIES):
            assert not is_vendor_noise("OpenAI", category), category
            assert not is_vendor_noise("Microsoft", category), category

    def test_unknown_category_still_blocks(self) -> None:
        """category 不明はベンダ扱い (地図に偽の点を出さない側へ倒す)。"""
        assert is_vendor_noise("Cisco", None)
        assert is_vendor_noise("Cisco", "")
