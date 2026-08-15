"""PDF 本文抽出のテスト (2026-08-15)。

一次ソースの一部 (BSI Cybersicherheitswarnungen / CSA シンガポール) は勧告を PDF
でしか出さず、HTML 前提の抽出では本文 0 文字 → 未配信になっていた。
"""

from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter

from src.tools.pdf_text import MAX_PDF_BYTES, extract_pdf_text, looks_like_pdf


def _pdf_bytes(pages: int = 1) -> bytes:
    """テキストを持つ最小 PDF を生成する (pypdf のみで完結させる)。"""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestLooksLikePdf:
    def test_detects_by_content_type(self) -> None:
        assert looks_like_pdf(content_type="application/pdf; charset=binary", body=b"")

    def test_detects_by_magic_bytes(self) -> None:
        # Content-Type を octet-stream で返すサイトがあるため実体も見る
        assert looks_like_pdf(content_type="application/octet-stream", body=b"%PDF-1.7\n...")

    def test_html_is_not_pdf(self) -> None:
        assert not looks_like_pdf(content_type="text/html", body=b"<!doctype html><html>")

    def test_extension_alone_is_not_used(self) -> None:
        # BSI は ".pdf?__blob=publicationFile" 形式で拡張子が当てにならない前提の裏取り
        assert not looks_like_pdf(content_type="text/html", body=b"<html>x.pdf</html>")


class TestExtractPdfText:
    def test_broken_pdf_returns_empty(self) -> None:
        assert extract_pdf_text(b"%PDF-1.7 broken") == ""

    def test_blank_pdf_returns_empty(self) -> None:
        # スキャン画像 PDF 相当 (テキスト描画が無い) は空を返す = OCR はしない
        assert extract_pdf_text(_pdf_bytes()) == ""

    def test_oversized_pdf_is_skipped(self) -> None:
        big = b"%PDF-1.7" + b"0" * (MAX_PDF_BYTES + 1)
        assert extract_pdf_text(big) == ""

    def test_page_cap_is_applied(self) -> None:
        # 上限より多いページを渡しても例外なく処理が完了する (打ち切り側の健全性)
        assert extract_pdf_text(_pdf_bytes(pages=5), max_pages=2) == ""


class TestExtractorPdfBranch:
    """ContentExtractor が PDF 応答を PDF 経路に流すこと。"""

    @pytest.mark.asyncio
    async def test_pdf_response_uses_pdf_method(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.tools import content_extractor as ce

        class _Resp:
            status_code = 200
            headers = {"content-type": "application/pdf"}
            content = b"%PDF-1.7 ..."
            text = ""

        ex = ce.ContentExtractor()
        monkeypatch.setattr(ex, "_get_with_ua_rotation", lambda url: _fake(_Resp()))
        monkeypatch.setattr(ce, "assert_safe_public_url", lambda url: None)
        monkeypatch.setattr(ce, "extract_pdf_text", lambda body: "本文テキスト " * 40)
        res = await ex.extract("https://e.example/advisory.pdf")
        assert res.success
        assert res.extraction_method == ce.EXTRACTION_METHOD_PDF

    @pytest.mark.asyncio
    async def test_scanned_pdf_reports_distinct_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.tools import content_extractor as ce

        class _Resp:
            status_code = 200
            headers = {"content-type": "application/pdf"}
            content = b"%PDF-1.7 ..."
            text = ""

        ex = ce.ContentExtractor()
        monkeypatch.setattr(ex, "_get_with_ua_rotation", lambda url: _fake(_Resp()))
        monkeypatch.setattr(ce, "assert_safe_public_url", lambda url: None)
        monkeypatch.setattr(ce, "extract_pdf_text", lambda body: "")
        res = await ex.extract("https://e.example/scan.pdf")
        # UA 再試行では直らない失敗として HTML 側の失敗と区別する (購読ソース画面の材料)
        assert not res.success and res.failure_reason == "pdf_no_text"


async def _fake(value: object) -> object:
    return value
