"""src.tools.text_sanitizer のテスト (Phase 5L-2)。"""

from __future__ import annotations

from src.tools.text_sanitizer import (
    cut_json_tail,
    has_html_residue,
    has_json_tail,
    sanitize_for_display,
)


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


class TestJsonTailCut:
    """LLM が JSON 文字列を閉じ損ねると後続フィールドが値へ流れ込む (2026-08-18)。

    本番 33 件で発生し、300 字 truncate のせいで「少し長い対処」に見えて 2 か月
    検知できなかった。**先頭の正しい 1 文は救い、署名以降だけ捨てる**。
    """

    def test_cuts_the_leaked_json_continuation(self) -> None:
        text = (
            '最新バージョンへ更新すること。 ",\n'
            '  "article_type": "press",\n'
            '  "routing_flags": {\n    "japan_targeted": false,'
        )

        assert cut_json_tail(text) == "最新バージョンへ更新すること。"

    def test_cuts_when_separator_is_inline(self) -> None:
        """区切りが改行を挟まない形もある (実データより)。"""
        text = 'カスタムドメインへ移行を検討すべきである。\\n\\n\',"analyst_note": "注意が必要。"'

        assert cut_json_tail(text) == "カスタムドメインへ移行を検討すべきである。"

    def test_keeps_prose_with_quotes_and_colons(self) -> None:
        """日本語の散文は切らない — 引用符もコロンも普通に出る。"""
        text = 'OpenSSL を更新し、"Trusted Clients" 等で範囲を制限。注意: 再起動が必要。'

        assert cut_json_tail(text) == text
        assert has_json_tail(text) is False

    def test_detects_residue_for_audit(self) -> None:
        assert has_json_tail('対処。 ", "article_type": "breaking"') is True
        assert has_json_tail("") is False
        assert has_json_tail(None) is False

    def test_empty_input_is_empty_string(self) -> None:
        assert cut_json_tail(None) == ""
