"""src.cti.dedup_key のテスト (Phase 5L-4)。"""

from __future__ import annotations

from src.cti.dedup_key import compute_dedup_key


class TestComputeDedupKey:
    def test_llm_key_takes_priority(self) -> None:
        """LLM が出した key を最優先 (CVE-ID 抽出より上位)。"""
        result = compute_dedup_key(
            llm_key="salt-typhoon-supplychain-ibm-italy",
            title="CVE-2026-12345 details",
            body_first_line="exploited",
            article_id_fallback="aid-1",
        )
        assert result == "salt-typhoon-supplychain-ibm-italy"

    def test_cve_id_extracted_from_title(self) -> None:
        result = compute_dedup_key(
            llm_key="",
            title="Apache CVE-2026-23918 RCE が発覚",
            article_id_fallback="aid-2",
        )
        assert result == "cve-2026-23918"

    def test_cve_id_extracted_from_body(self) -> None:
        result = compute_dedup_key(
            llm_key="",
            title="脆弱性レポート",
            body_first_line="調査により CVE-2026-1234 が確認された",
            article_id_fallback="aid-3",
        )
        assert result == "cve-2026-1234"

    def test_fallback_uses_article_id_for_uniqueness(self) -> None:
        """LLM/CVE 不在 → article_id ハッシュで一意化 (誤クラスタリング防止)。"""
        a = compute_dedup_key(llm_key="", title="同じタイトル", article_id_fallback="article-1")
        b = compute_dedup_key(llm_key="", title="同じタイトル", article_id_fallback="article-2")
        assert a != b
        # title slug 部分は同じだが suffix で区別される
        assert a.startswith(b.split("-")[0])  # 共通プレフィックス確認

    def test_cve_id_case_insensitive(self) -> None:
        result = compute_dedup_key(
            llm_key="",
            title="Apache cve-2026-9999 RCE",
            article_id_fallback="aid",
        )
        assert result == "cve-2026-9999"

    def test_japanese_title_slug(self) -> None:
        """非 ASCII タイトルは文字をそのまま slug に保持。"""
        result = compute_dedup_key(
            llm_key="",
            title="日本標的 APT 攻撃",
            article_id_fallback="aid-jp",
        )
        # 日本語をそのまま含み、article_id ハッシュ suffix が付く
        assert "日本標的" in result
        assert "-" in result  # suffix があることを確認

    def test_ascii_title_normalized_to_slug(self) -> None:
        result = compute_dedup_key(
            llm_key="",
            title="Salt Typhoon hits IBM Italy",
            article_id_fallback="aid",
        )
        assert result.startswith("salt-typhoon-hits-ibm-italy")

    def test_empty_inputs_return_article_id_based_fallback(self) -> None:
        """全部空でも article_id があれば一意なキーが出る。"""
        result = compute_dedup_key(llm_key="", title="", article_id_fallback="abc")
        # title 空でも article 部分 + hash で構成される
        assert result  # 空文字でない

    def test_llm_key_with_whitespace_is_trimmed(self) -> None:
        result = compute_dedup_key(llm_key="  cve-2026-1  ", title="t", article_id_fallback="aid")
        assert result == "cve-2026-1"


class TestExtractCveId:
    """Phase 5T-V-2: 正規化 CVE-ID 抽出のテスト。"""

    def test_extracts_from_simple_key(self) -> None:
        from src.cti.dedup_key import extract_cve_id

        assert extract_cve_id(dedup_key="cve-2026-0300") == "cve-2026-0300"

    def test_extracts_from_key_with_suffix(self) -> None:
        """LLM 不安定問題: suffix 付き key からも同じ CVE-ID を抽出。"""
        from src.cti.dedup_key import extract_cve_id

        assert extract_cve_id(dedup_key="cve-2026-0300-palo-alto-rce") == "cve-2026-0300"
        assert extract_cve_id(dedup_key="cve-2026-0300-palo-alto-pan-os-rce") == "cve-2026-0300"

    def test_extracts_from_title_when_no_key(self) -> None:
        from src.cti.dedup_key import extract_cve_id

        assert (
            extract_cve_id(dedup_key=None, title="CVE-2026-42897 Exchange RCE") == "cve-2026-42897"
        )

    def test_returns_none_when_no_cve(self) -> None:
        from src.cti.dedup_key import extract_cve_id

        assert extract_cve_id(dedup_key="shinyhunters-breach", title="Some breach") is None

    def test_case_insensitive(self) -> None:
        from src.cti.dedup_key import extract_cve_id

        assert extract_cve_id(dedup_key="CVE-2026-1234") == "cve-2026-1234"

    def test_handles_empty_inputs(self) -> None:
        from src.cti.dedup_key import extract_cve_id

        assert extract_cve_id(dedup_key=None, title=None) is None
        assert extract_cve_id(dedup_key="", title="") is None
