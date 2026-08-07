"""本文抽出の JS チャレンジ発火 + Playwright per-run cap のテスト (2026-08-01)。

ipdefenseforum 型 (307 + チャレンジ本文) が status 集合ベースの発火条件をすり抜けて
Playwright 経路が一度も発火しなかった事故の回帰防止。発火は status でなく
**応答の実態 (指紋)** でも判断し、試行回数は per-run cap で時間予算と両立させる。
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

import httpx
import pytest

from src.tools.content_extractor import (
    ContentExtractor,
    ExtractionResult,
    _playwright_cap_from_env,
)

CHALLENGE_HTML = (
    "<html><title>You are being redirected...</title>"
    "<noscript>Javascript is required. Please enable javascript before you are "
    "allowed to see this page.</noscript><script>var s={}</script></html>"
)


def _extractor(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    cap: int | None = None,
) -> ContentExtractor:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": "TestUA/1.0"}
    )
    return ContentExtractor(client=client, user_agent="TestUA/1.0", playwright_attempt_cap=cap)


def _pw_success(url: str = "https://example.com/a") -> ExtractionResult:
    return ExtractionResult(
        url=url,
        text="rendered body " * 30,
        success=True,
        extraction_method="playwright",
    )


class TestJsChallengeTrigger:
    @pytest.mark.asyncio
    async def test_307_challenge_fires_playwright(self) -> None:
        # 307 は block status 集合外 — 指紋で発火することの確認 (ipdefenseforum 型)
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(307, text=CHALLENGE_HTML)

        async with _extractor(handler) as ex:
            mock = AsyncMock(return_value=_pw_success())
            ex._extract_with_playwright = mock  # type: ignore[method-assign]
            result = await ex.extract("https://example.com/a")

        assert result.success is True
        assert mock.await_count == 1

    @pytest.mark.asyncio
    async def test_307_challenge_playwright_failure_reports_js_challenge(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(307, text=CHALLENGE_HTML)

        async with _extractor(handler) as ex:
            ex._extract_with_playwright = AsyncMock(return_value=None)  # type: ignore[method-assign]
            result = await ex.extract("https://example.com/a")

        # UA では直らない失敗として http_error_307 でなく js_challenge を報告する
        assert result.success is False
        assert result.failure_reason == "js_challenge"

    @pytest.mark.asyncio
    async def test_plain_redirect_without_challenge_does_not_fire(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(307, text="<html>moved</html>")

        async with _extractor(handler) as ex:
            mock = AsyncMock(return_value=_pw_success())
            ex._extract_with_playwright = mock  # type: ignore[method-assign]
            result = await ex.extract("https://example.com/a")

        assert result.failure_reason == "http_error_307"
        assert mock.await_count == 0

    @pytest.mark.asyncio
    async def test_block_status_still_fires(self) -> None:
        # 既存挙動の維持: 403 (block status) は指紋なしでも発火する
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="forbidden")

        async with _extractor(handler) as ex:
            mock = AsyncMock(return_value=_pw_success())
            ex._extract_with_playwright = mock  # type: ignore[method-assign]
            result = await ex.extract("https://example.com/a")

        assert result.success is True
        assert mock.await_count == 1


class TestPlaywrightAttemptCap:
    @pytest.mark.asyncio
    async def test_cap_limits_attempts_per_run(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="forbidden")

        async with _extractor(handler, cap=2) as ex:
            mock = AsyncMock(return_value=None)
            ex._extract_with_playwright = mock  # type: ignore[method-assign]
            for i in range(4):
                result = await ex.extract(f"https://example.com/{i}")
            # cap 超過後も既存の failure 経路で正常に fail する
            assert result.failure_reason == "http_error_403"

        assert mock.await_count == 2

    @pytest.mark.asyncio
    async def test_zero_cap_means_unlimited(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="forbidden")

        async with _extractor(handler, cap=0) as ex:
            mock = AsyncMock(return_value=None)
            ex._extract_with_playwright = mock  # type: ignore[method-assign]
            for i in range(3):
                await ex.extract(f"https://example.com/{i}")

        assert mock.await_count == 3

    def test_cap_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLAYWRIGHT_EXTRACT_CAP", "25")
        assert _playwright_cap_from_env() == 25
        monkeypatch.setenv("PLAYWRIGHT_EXTRACT_CAP", "not-a-number")
        assert _playwright_cap_from_env() == 10  # 既定値に fallback
        monkeypatch.delenv("PLAYWRIGHT_EXTRACT_CAP")
        assert _playwright_cap_from_env() == 10


class TestShortPageAcceptance:
    """短文ページの汎用受理 (2026-08-01、JVN 型の救済)。

    公式 advisory はページ自体が短く、抽出成功なのに一律閾値で捨てられていた
    (JVNVU 189 字 / min 200)。閾値の 7 割以上あれば「短いが完全なページ」として採用。
    """

    def _handler(self) -> Callable[[httpx.Request], httpx.Response]:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html><body><p>page</p></body></html>")

        return handler

    def _patch_trafilatura(self, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        import trafilatura

        monkeypatch.setattr(trafilatura, "extract", lambda *a, **k: text)

    @pytest.mark.asyncio
    async def test_short_but_complete_page_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 189 字 (JVN 実測値) >= 200 * 0.7 = 140 → 受理
        body = "あ" * 189
        self._patch_trafilatura(monkeypatch, body)
        async with _extractor(self._handler()) as ex:
            ex._extract_with_playwright = AsyncMock(return_value=None)  # type: ignore[method-assign]
            result = await ex.extract("https://example.com/vu/1")

        assert result.success is True
        assert result.text == body

    @pytest.mark.asyncio
    async def test_fragment_below_ratio_still_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_trafilatura(monkeypatch, "あ" * 100)  # 100 < 140
        async with _extractor(self._handler()) as ex:
            ex._extract_with_playwright = AsyncMock(return_value=None)  # type: ignore[method-assign]
            result = await ex.extract("https://example.com/frag")

        assert result.success is False
        assert result.failure_reason == "content_too_short"

    @pytest.mark.asyncio
    async def test_paywall_snippet_is_not_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 長さは受理域でも paywall 疑いは従来どおり弾く (受理は paywall 判定の後段)
        snippet = ("subscribe to read the full article. " * 6)[:150]
        self._patch_trafilatura(monkeypatch, snippet)
        async with _extractor(self._handler()) as ex:
            ex._extract_with_playwright = AsyncMock(return_value=None)  # type: ignore[method-assign]
            result = await ex.extract("https://example.com/paywalled")

        assert result.success is False
        assert result.failure_reason == "paywall_suspected"

    @pytest.mark.asyncio
    async def test_playwright_full_text_preferred_over_short_acceptance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Playwright が全文を取れたらそちらを優先 (受理は最後の手段)
        self._patch_trafilatura(monkeypatch, "あ" * 189)
        full = _pw_success()
        async with _extractor(self._handler()) as ex:
            ex._extract_with_playwright = AsyncMock(return_value=full)  # type: ignore[method-assign]
            result = await ex.extract("https://example.com/js-heavy")

        assert result.extraction_method == "playwright"
        assert result.text == full.text
