"""src.tools.text_sanitizer のテスト (Phase 5L-2)。"""

from __future__ import annotations

from src.tools.text_sanitizer import has_html_residue, sanitize_for_display


class TestSanitizeForDisplay:
    def test_strips_html_tags(self) -> None:
        assert sanitize_for_display("Conti が逮捕</td>") == "Conti が逮捕"

    def test_strips_complex_html_tags(self) -> None:
        result = sanitize_for_display('<a href="x">link</a> text')
        assert result == "link text"

    def test_decodes_html_entities(self) -> None:
        assert sanitize_for_display("AT&amp;T が侵害") == "AT&T が侵害"
        assert sanitize_for_display("&lt;script&gt;") == "<script>"

    def test_normalizes_unicode_nfkc(self) -> None:
        # 全角英数 → 半角英数
        assert sanitize_for_display("ＡＰＴ４１") == "APT41"

    def test_strips_control_characters(self) -> None:
        assert sanitize_for_display("hello\x00world") == "helloworld"
        assert sanitize_for_display("\x01\x02fragment\x07") == "fragment"

    def test_collapse_whitespace_optional(self) -> None:
        assert sanitize_for_display("  a   b  ", collapse_whitespace=True) == "a b"
        # collapse=False ならそのまま
        assert sanitize_for_display("  a   b  ", collapse_whitespace=False) == "  a   b  "

    def test_max_length_truncation(self) -> None:
        text = "x" * 200
        result = sanitize_for_display(text, max_length=100)
        assert len(result) == 100

    def test_none_returns_empty(self) -> None:
        assert sanitize_for_display(None) == ""
        assert sanitize_for_display("") == ""

    def test_preserves_japanese_text(self) -> None:
        text = "日本企業へのサプライチェーン攻撃"
        assert sanitize_for_display(text) == text

    def test_realistic_feed_title_with_td_residue(self) -> None:
        """RSS feed で観測された </td> 末尾混入の典型例。"""
        text = "Conti および Akira の提携者が 8 年の禁錮刑</td>"
        assert sanitize_for_display(text) == "Conti および Akira の提携者が 8 年の禁錮刑"


class TestHasHtmlResidue:
    def test_detects_html_tags(self) -> None:
        assert has_html_residue("foo<a>bar</a>") is True
        assert has_html_residue("text</td>") is True

    def test_detects_html_entities(self) -> None:
        assert has_html_residue("AT&amp;T") is True
        assert has_html_residue("&#39;quote&#39;") is True

    def test_clean_text_returns_false(self) -> None:
        assert has_html_residue("plain text") is False
        assert has_html_residue("日本企業の発表") is False

    def test_none_or_empty_returns_false(self) -> None:
        assert has_html_residue(None) is False
        assert has_html_residue("") is False
