"""src.tools.content_extractor のテスト (Step 5)。

httpx.MockTransport で fetch をモックする。trafilatura の抽出ロジック
そのものはライブラリ任せなので、本テストは「結果ハンドリング」
(成功・失敗カテゴリ・メタデータ) の検証に集中する。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from src.tools.content_extractor import (
    EXTRACTION_METHOD_TRAFILATURA,
    PAYWALL_KEYWORDS,
    ContentExtractor,
    ExtractionResult,
    _extract_html_lang,
    _looks_like_paywall,
    _parse_iso_date,
)

# ---------- ヘルパ ----------


def _build_extractor(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    min_content_length: int = 200,
    user_agent: str = "TestUA/1.0",
) -> ContentExtractor:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, headers={"User-Agent": user_agent})
    return ContentExtractor(
        min_content_length=min_content_length,
        user_agent=user_agent,
        client=client,
    )


def _full_article_html(
    body_paragraph: str = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. ",
    repeat: int = 30,
    title: str = "Test Article Title",
    lang: str = "en",
    author: str = "Test Author",
    iso_date: str = "2023-11-14",
) -> str:
    body = body_paragraph * repeat
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8">
  <title>{title}</title>
  <meta name="author" content="{author}">
  <meta name="date" content="{iso_date}">
  <meta property="article:published_time" content="{iso_date}T12:00:00Z">
</head>
<body>
  <article>
    <h1>{title}</h1>
    <p>{body}</p>
    <p>{body}</p>
  </article>
</body>
</html>"""


def _short_html(body: str = "短い本文。", lang: str = "ja") -> str:
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head><title>Short</title></head>
<body><article><p>{body}</p></article></body>
</html>"""


def _paywall_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head><title>Paywalled article</title></head>
<body>
  <article>
    <h1>Article behind paywall</h1>
    <p>This is the lede paragraph that is short and teases the article.</p>
    <p>Subscribe to continue reading this article.</p>
  </article>
</body>
</html>"""


# ---------- ExtractionResult / model ----------


class TestExtractionResult:
    def test_default_method_is_trafilatura(self) -> None:
        r = ExtractionResult(url="https://example.com", success=False)
        assert r.extraction_method == EXTRACTION_METHOD_TRAFILATURA

    def test_is_frozen(self) -> None:
        r = ExtractionResult(url="https://example.com", success=True, text="x")
        with pytest.raises(Exception):  # noqa: B017, BLE001
            r.text = "mutated"


# ---------- 単体ヘルパ ----------


class TestHtmlLang:
    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            ('<html lang="en">', "en"),
            ("<html lang='en-US'>", "en"),
            ('<html LANG="JA">', "ja"),
            ('<html dir="ltr" lang="zh-CN" class="x">', "zh"),
            ("<html>", None),
            ("<HTML>", None),
        ],
    )
    def test_extracts_lang_from_html_tag(self, html: str, expected: str | None) -> None:
        assert _extract_html_lang(html) == expected


class TestParseIsoDate:
    def test_parses_iso_with_z(self) -> None:
        d = _parse_iso_date("2023-11-14T12:00:00Z")
        assert d is not None
        assert d.year == 2023

    def test_parses_iso_with_offset(self) -> None:
        d = _parse_iso_date("2023-11-14T12:00:00+09:00")
        assert d is not None

    def test_returns_none_for_garbage(self) -> None:
        assert _parse_iso_date("not a date") is None
        assert _parse_iso_date("") is None
        assert _parse_iso_date(None) is None


class TestPaywallDetection:
    def test_detects_english_keyword(self) -> None:
        assert _looks_like_paywall(
            text="short teaser",
            html="<html><body>Subscribe to continue reading</body></html>",
        )

    def test_detects_japanese_keyword(self) -> None:
        assert _looks_like_paywall(
            text="続きをお読みいただくには登録が必要です",
            html="<html></html>",
        )

    def test_returns_false_when_no_keyword(self) -> None:
        assert not _looks_like_paywall(
            text="completely free content here",
            html="<html><body>free</body></html>",
        )

    def test_keyword_list_is_non_empty(self) -> None:
        assert len(PAYWALL_KEYWORDS) > 0


# ---------- extract: 成功系 ----------


class TestExtractSuccess:
    @pytest.mark.asyncio
    async def test_extracts_long_article_successfully(self) -> None:
        html = _full_article_html()

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        async with _build_extractor(handler) as ex:
            result = await ex.extract("https://example.com/article")

        assert result.success is True
        assert result.failure_reason is None
        assert len(result.text) >= 200
        assert "Lorem ipsum" in result.text
        # 言語: trafilatura が返さない場合でも HTML lang 属性から拾えること
        assert result.language == "en"

    @pytest.mark.asyncio
    async def test_japanese_article_lang_detected(self) -> None:
        html = _full_article_html(
            body_paragraph="この記事は日本語のテストです。文章を十分な長さにします。",
            repeat=10,
            lang="ja-JP",
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        async with _build_extractor(handler) as ex:
            result = await ex.extract("https://example.com/ja")

        assert result.success is True
        assert result.language == "ja"

    @pytest.mark.asyncio
    async def test_user_agent_is_sent(self) -> None:
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.headers.get("User-Agent", ""))
            return httpx.Response(200, text=_full_article_html())

        async with _build_extractor(handler, user_agent="kuebiko/0.1") as ex:
            await ex.extract("https://example.com/")

        assert seen and seen[0] == "kuebiko/0.1"


# ---------- extract: HTTP 系の失敗 ----------


class TestExtractHttpFailures:
    @pytest.mark.asyncio
    async def test_404_returns_http_error_404(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        async with _build_extractor(handler) as ex:
            result = await ex.extract("https://example.com/missing")

        assert result.success is False
        assert result.failure_reason == "http_error_404"

    @pytest.mark.asyncio
    async def test_503_returns_http_error_503(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        async with _build_extractor(handler) as ex:
            result = await ex.extract("https://example.com/")

        assert result.failure_reason == "http_error_503"

    @pytest.mark.asyncio
    async def test_timeout_categorized(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated timeout")

        async with _build_extractor(handler) as ex:
            result = await ex.extract("https://example.com/")

        assert result.failure_reason == "timeout"

    @pytest.mark.asyncio
    async def test_connection_error_categorized(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated DNS failure")

        async with _build_extractor(handler) as ex:
            result = await ex.extract("https://example.com/")

        assert result.failure_reason == "connection_error"


# ---------- extract: 抽出失敗 / 短い / ペイウォール ----------


class TestExtractContentFailures:
    @pytest.mark.asyncio
    async def test_empty_html_extraction_fails(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html></html>")

        async with _build_extractor(handler) as ex:
            result = await ex.extract("https://example.com/")

        assert result.success is False
        assert result.failure_reason == "extraction_failed"

    @pytest.mark.asyncio
    async def test_short_content_categorized(self) -> None:
        # 抽出はできるが本文が min_content_length 未満
        html = _short_html(
            body="一文だけの極端に短い本文サンプルです。だが特定キーワードは含まれない。",
        )

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        async with _build_extractor(handler, min_content_length=500) as ex:
            result = await ex.extract("https://example.com/short")

        # 内容次第で extraction_failed か content_too_short どちらかになるが、
        # 「成功扱いにはならない」ことを保証する。
        assert result.success is False
        assert result.failure_reason in ("content_too_short", "extraction_failed")

    @pytest.mark.asyncio
    async def test_paywall_suspected_takes_priority_over_too_short(self) -> None:
        html = _paywall_html()

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        async with _build_extractor(handler, min_content_length=500) as ex:
            result = await ex.extract("https://example.com/paywall")

        assert result.success is False
        assert result.failure_reason in ("paywall_suspected", "extraction_failed")


# ---------- 接続プール / 複数呼び出し ----------


class TestPoolReuse:
    @pytest.mark.asyncio
    async def test_multiple_extracts_share_underlying_client(self) -> None:
        """同じ ContentExtractor で複数 URL を処理できる (接続プール再利用)。"""
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, text=_full_article_html())

        async with _build_extractor(handler) as ex:
            results = [await ex.extract(f"https://example.com/article-{i}") for i in range(3)]

        assert all(r.success for r in results)
        assert call_count == 3
