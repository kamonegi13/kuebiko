"""LLM 自由記述 metadata の HTML 残渣除去テスト (2026-08-15)。

remediation に本文末尾の閉じタグ列 (`</p></div>...`) が混入し UI に露出した
実障害の回帰固定。個別 sanitize の漏れを防ぐためキー集合を SSoT 化した。
"""

from __future__ import annotations

from src.pipeline.briefing import LLM_FREE_TEXT_METADATA_KEYS
from src.tools.text_sanitizer import has_html_residue, sanitize_for_display


class TestFreeTextKeys:
    def test_remediation_is_covered(self) -> None:
        # 実障害が出たフィールド (61 記事に閉じタグ列が残っていた)
        assert "remediation" in LLM_FREE_TEXT_METADATA_KEYS

    def test_other_llm_narrative_fields_covered(self) -> None:
        for key in ("technical_axis_summary", "socio_political_rationale"):
            assert key in LLM_FREE_TEXT_METADATA_KEYS


class TestSanitizeRemovesResidue:
    def test_trailing_close_tag_run_removed(self) -> None:
        raw = "不審なURLへのアクセスを行わないこと。 </p>" + "</div>" * 25
        out = sanitize_for_display(raw)
        assert out.strip() == "不審なURLへのアクセスを行わないこと。"
        assert has_html_residue(out) is False

    def test_html_document_tail_removed(self) -> None:
        raw = "最新版へ更新すること。</p></div></body></html>"
        assert sanitize_for_display(raw) == "最新版へ更新すること。"

    def test_plain_text_unchanged(self) -> None:
        raw = "Adobe の勧告を参照し修正プログラムを適用すること。"
        assert sanitize_for_display(raw) == raw


class TestTruncatedTagTail:
    def test_partial_tag_at_end_removed(self) -> None:
        # 300 字 truncate が `</div>` を `</d` に切った残骸 (実データで 4 件)
        assert (
            sanitize_for_display("パッチを適用してください。</d").strip()
            == "パッチを適用してください。"
        )
        assert sanitize_for_display("確認すること</div").strip() == "確認すること"

    def test_less_than_in_prose_preserved(self) -> None:
        # 数式・比較記号は壊さない (末尾の `<` 単独やタグ名を伴わない `<` は残す)
        assert sanitize_for_display("影響は 5% 未満 (< 5%)") == "影響は 5% 未満 (< 5%)"

    def test_partial_tag_with_underscore_removed(self) -> None:
        # LLM が JSON schema 名を吐いた残骸 (`</is_ransomware` 途中切れ) も末尾断片として除去
        assert sanitize_for_display("再確認が重要である。</is_").strip() == "再確認が重要である。"
