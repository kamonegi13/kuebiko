"""PDF からの本文テキスト抽出 (2026-08-15)。

一次ソースの一部は勧告を **PDF でしか出さない** (BSI Cybersicherheitswarnungen、
CSA シンガポール等)。trafilatura は HTML 専用なので、こうした記事は本文 0 文字で
extract_failed に終端し、要約も重要度判定も行われないまま 1 件も配信されなかった。

スキャン画像 PDF は対象外 (OCR はしない)。テキスト PDF だけを読む。
**上限を先に決めてから読む**: 巨大 PDF / ページ数の多い年次報告書で 1 記事が
run 時間とメモリを食い潰さないようにする (取得は毎時走る)。
"""

from __future__ import annotations

import re

from src.logging_config import get_logger

_log = get_logger(__name__)

__all__ = ["MAX_PDF_BYTES", "MAX_PDF_PAGES", "extract_pdf_text", "looks_like_pdf"]

# これ以上大きい PDF は読まない (年次報告書・カタログ等。勧告は通常 1MB 未満)。
MAX_PDF_BYTES = 20 * 1024 * 1024
# 先頭からこのページ数だけ読む (勧告は数ページ。長文は冒頭で足りる)。
MAX_PDF_PAGES = 30
# 抽出テキストの上限 (LLM プロンプトに載る量の上限は下流でも掛かるが、ここでも歯止め)。
MAX_PDF_CHARS = 120_000

_PDF_MAGIC = b"%PDF-"
_WS = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")


def looks_like_pdf(*, content_type: str, body: bytes) -> bool:
    """PDF かを判定する。Content-Type と実体の両方を見る。

    Content-Type を octet-stream で返すサイトがあるため、マジックバイトも見る
    (逆に拡張子は当てにしない — BSI は ``.pdf?__blob=publicationFile`` 形式)。
    """
    if "application/pdf" in content_type.lower():
        return True
    return body[:5] == _PDF_MAGIC


def extract_pdf_text(body: bytes, *, max_pages: int = MAX_PDF_PAGES) -> str:
    """PDF のテキストを返す。読めない / 上限超過なら空文字。

    例外は投げない (本文抽出は失敗しても pipeline を止めない方針)。
    """
    if len(body) > MAX_PDF_BYTES:
        _log.info("pdf_extract_skipped_too_large", size=len(body), limit=MAX_PDF_BYTES)
        return ""
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(body))
        pages = reader.pages[:max_pages]
        chunks = [page.extract_text() or "" for page in pages]
    except Exception as e:  # noqa: BLE001 — 破損 PDF / 暗号化 PDF で pipeline を止めない
        _log.info("pdf_extract_failed", error=f"{type(e).__name__}: {e}")
        return ""
    text = _BLANKS.sub("\n\n", _WS.sub(" ", "\n".join(chunks))).strip()
    return text[:MAX_PDF_CHARS]
