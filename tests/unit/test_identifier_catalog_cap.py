"""識別子カタログの提示上限 (2026-09-21)。

IOC を大量に列挙する記事 (本文 315k 字・カタログ 84k 字) がメンバーにいると、事象ニュースの
プロンプトが 60k tok に膨らみ 1 件 10 分超 + 出力暴走になった (毎時の統合の段が 2 度 timeout)。
提示は上限つき、**番号は振り直さない** (照合は全件カタログで行うので {In} は解決できる)。
"""

from __future__ import annotations

from src.tools.identifier_catalog import build_catalog, render_catalog


def _texts() -> list[str]:
    hashes = " ".join(f"{i:064x}" for i in range(1, 31))  # sha256 ×30
    return [f"CVE-2026-1234 を悪用。C2 は evil.example と 203.0.113.5。{hashes}", "version 1.8.6"]


class TestRenderCatalogCap:
    def test_without_cap_everything_is_shown(self) -> None:
        cat = build_catalog(_texts())
        text = render_catalog(cat)
        assert text.count("\n") + 1 == len([e for e in cat.entries if e.token])

    def test_cap_keeps_important_kinds_first_and_original_tokens(self) -> None:
        cat = build_catalog(_texts())
        text = render_catalog(cat, max_entries=5)
        lines = [ln for ln in text.splitlines() if " = " in ln]
        assert len(lines) == 5
        kinds = [ln.split("(")[1].split(",")[0] for ln in lines]
        # 重要な型 (cve / version) と固有の型 (ip) は残り、bulk の hash があふれる
        assert {"cve", "version", "ip"} <= set(kinds)
        assert kinds.count("hash") == 5 - 3
        # ⭐ 番号は元のまま (I1 が cve とは限らないが、全件カタログの token と一致する)
        by_token = cat.by_token
        for ln in lines:
            token, value = ln.split(" = ", 1)
            assert by_token[token].identifier.raw == value.split("  (")[0]
        assert "省略" in text and "直書き" in text

    def test_cap_not_reached_adds_no_note(self) -> None:
        cat = build_catalog(["CVE-2026-1 と CVE-2026-2"])
        assert "省略" not in render_catalog(cat, max_entries=10)


class TestOverflowSummary:
    def test_overflow_is_summarized_per_kind_with_member_refs(self) -> None:
        """あふれた分は「型ごとの件数 + 記事番号」で残す (値は本文参照)。
        どの記事にどの型の IOC が何件あるかは落とさない (利用者の要望、2026-09-21)。"""
        cat = build_catalog(_texts())
        text = render_catalog(cat, max_entries=3)
        shown = [ln for ln in text.splitlines() if " = " in ln]
        assert len(shown) == 3
        total = len([e for e in cat.entries if e.token])
        assert f"他 {total - 3} 件" in text
        assert "hash " in text and "記事 [1]" in text  # 型ごとの内訳と記事番号
