"""src.grok.fetcher のテスト (Phase 2)。

Playwright の実ブラウザは起動せず、unittest.mock でブラウザ層を差し替える。
ログインリダイレクトのヘルパ関数は単体で検証する。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.grok.fetcher import (
    LOGIN_REDIRECT_MARKERS,
    GrokFetcher,
    GrokFetchError,
    _looks_like_login_redirect,
)


class TestLoginRedirectDetection:
    @pytest.mark.parametrize(
        "url",
        [
            "https://grok.com/login",
            "https://x.com/i/flow/login",
            "https://example.com/auth/callback",
            "https://provider.com/oauth/authorize?client=x",
        ],
    )
    def test_detects_login_url(self, url: str) -> None:
        assert _looks_like_login_redirect(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://grok.com/share/abc123",
            "https://example.com/article",
            "https://grok.com/conversation/xyz",
        ],
    )
    def test_normal_url_not_detected(self, url: str) -> None:
        assert not _looks_like_login_redirect(url)

    def test_markers_are_lowercase(self) -> None:
        for marker in LOGIN_REDIRECT_MARKERS:
            assert marker == marker.lower()


class TestGrokFetcher:
    @pytest.mark.asyncio
    async def test_fetch_without_start_raises(self) -> None:
        fetcher = GrokFetcher()
        with pytest.raises(GrokFetchError, match="start"):
            await fetcher.fetch("https://grok.com/share/x")

    @pytest.mark.asyncio
    async def test_fetch_returns_html_on_success(self, tmp_path: Path) -> None:
        # Playwright API を全部 MagicMock で差し替え
        page = MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(status=200))
        page.wait_for_timeout = AsyncMock()
        page.content = AsyncMock(return_value="<html><body>report</body></html>")
        page.title = AsyncMock(return_value="Grok report A")
        page.url = "https://grok.com/share/abc"

        ctx = MagicMock()
        ctx.set_default_navigation_timeout = MagicMock()
        ctx.add_init_script = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=ctx)
        browser.close = AsyncMock()

        fetcher = GrokFetcher(state_path=tmp_path / "state.json")
        fetcher._browser = browser  # 直接代入 (start をスキップ)

        result = await fetcher.fetch("https://grok.com/share/abc")
        assert result.success is True
        assert "report" in result.html
        assert result.title == "Grok report A"

    @pytest.mark.asyncio
    async def test_fetch_extracts_body_text_via_dom(self, tmp_path: Path) -> None:
        """DOM selector で hydration 後の本文を抽出して body_text に格納する。"""
        page = MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(status=200))
        page.wait_for_timeout = AsyncMock()
        page.content = AsyncMock(return_value="<html>spa shell</html>")
        page.title = AsyncMock(return_value="Grok report")
        page.url = "https://grok.com/chat/x"

        # locator はチェイナブル: page.locator(sel).count() / .all_inner_texts()
        markdown_locator = MagicMock()
        markdown_locator.count = AsyncMock(return_value=2)
        markdown_locator.all_inner_texts = AsyncMock(
            return_value=["【全体概要】 first message", "【分類】 second message"],
        )

        empty_locator = MagicMock()
        empty_locator.count = AsyncMock(return_value=0)

        def locator_for(selector: str) -> MagicMock:
            if "markdown" in selector:
                return markdown_locator
            return empty_locator

        page.locator = MagicMock(side_effect=locator_for)

        ctx = MagicMock()
        ctx.set_default_navigation_timeout = MagicMock()
        ctx.add_init_script = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=ctx)
        browser.close = AsyncMock()

        fetcher = GrokFetcher(state_path=tmp_path / "state.json")
        fetcher._browser = browser

        result = await fetcher.fetch("https://grok.com/chat/x")
        assert result.success is True
        # 2 メッセージが \n\n で結合されている
        assert "【全体概要】 first message" in result.body_text
        assert "【分類】 second message" in result.body_text
        assert "\n\n" in result.body_text

    @pytest.mark.asyncio
    async def test_fetch_falls_back_when_no_body_selector_matches(
        self,
        tmp_path: Path,
    ) -> None:
        """全 selector マッチなしなら body_text は空文字列。"""
        page = MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(status=200))
        page.wait_for_timeout = AsyncMock()
        page.content = AsyncMock(return_value="<html>empty</html>")
        page.title = AsyncMock(return_value="x")
        page.url = "https://grok.com/chat/x"

        empty_locator = MagicMock()
        empty_locator.count = AsyncMock(return_value=0)
        page.locator = MagicMock(return_value=empty_locator)

        ctx = MagicMock()
        ctx.set_default_navigation_timeout = MagicMock()
        ctx.add_init_script = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=ctx)
        browser.close = AsyncMock()

        fetcher = GrokFetcher(state_path=tmp_path / "state.json")
        fetcher._browser = browser

        result = await fetcher.fetch("https://grok.com/chat/x")
        assert result.success is True
        assert result.body_text == ""

    @pytest.mark.asyncio
    async def test_fetch_detects_login_redirect(self, tmp_path: Path) -> None:
        page = MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(status=200))
        page.wait_for_timeout = AsyncMock()
        page.content = AsyncMock(return_value="<html>login form</html>")
        page.title = AsyncMock(return_value="Sign in")
        page.url = "https://grok.com/login"

        ctx = MagicMock()
        ctx.set_default_navigation_timeout = MagicMock()
        ctx.add_init_script = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=ctx)

        fetcher = GrokFetcher(state_path=tmp_path / "state.json")
        fetcher._browser = browser

        result = await fetcher.fetch("https://grok.com/share/abc")
        assert result.success is False
        assert result.failure_reason == "session_expired"

    @pytest.mark.asyncio
    async def test_fetch_returns_http_error_for_4xx(self, tmp_path: Path) -> None:
        page = MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(status=404))
        page.wait_for_timeout = AsyncMock()
        page.content = AsyncMock(return_value="not found")
        page.title = AsyncMock(return_value="404")
        page.url = "https://grok.com/share/missing"

        ctx = MagicMock()
        ctx.set_default_navigation_timeout = MagicMock()
        ctx.add_init_script = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=ctx)

        fetcher = GrokFetcher(state_path=tmp_path / "state.json")
        fetcher._browser = browser

        result = await fetcher.fetch("https://grok.com/share/missing")
        assert result.success is False
        assert result.failure_reason == "http_error_404"

    @pytest.mark.asyncio
    async def test_fetch_treats_401_as_session_expired(self, tmp_path: Path) -> None:
        """Phase 5P: 401 はセッション切れとして扱う (login redirect と同等)。"""
        page = MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(status=401))
        page.wait_for_timeout = AsyncMock()
        page.content = AsyncMock(return_value="unauth")
        page.title = AsyncMock(return_value="401")
        # final_url は redirect されないケース (status だけ 401)
        page.url = "https://grok.com/chat/abc"

        ctx = MagicMock()
        ctx.set_default_navigation_timeout = MagicMock()
        ctx.add_init_script = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=ctx)

        fetcher = GrokFetcher(state_path=tmp_path / "state.json")
        fetcher._browser = browser

        result = await fetcher.fetch("https://grok.com/chat/abc")
        assert result.success is False
        assert result.failure_reason == "session_expired"

    @pytest.mark.asyncio
    async def test_fetch_logs_grok_session_expired_401_event(
        self,
        tmp_path: Path,
    ) -> None:
        """Phase 5P: 401 検出時に grok_session_expired_401 ログが出る。"""
        from unittest.mock import patch

        page = MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(status=401))
        page.wait_for_timeout = AsyncMock()
        page.content = AsyncMock(return_value="")
        page.title = AsyncMock(return_value="")
        page.url = "https://grok.com/chat/abc"

        ctx = MagicMock()
        ctx.set_default_navigation_timeout = MagicMock()
        ctx.add_init_script = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=ctx)

        fetcher = GrokFetcher(state_path=tmp_path / "state.json")
        fetcher._browser = browser

        with patch("src.grok.fetcher._log") as mock_log:
            await fetcher.fetch("https://grok.com/chat/abc")

        events = [
            c
            for c in mock_log.warning.call_args_list
            if c.args and c.args[0] == "grok_session_expired_401"
        ]
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_fetch_403_remains_http_error_not_session_expired(
        self,
        tmp_path: Path,
    ) -> None:
        """403 は通常の http_error 扱い (401 との区別)。"""
        page = MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(status=403))
        page.wait_for_timeout = AsyncMock()
        page.content = AsyncMock(return_value="")
        page.title = AsyncMock(return_value="")
        page.url = "https://grok.com/chat/abc"

        ctx = MagicMock()
        ctx.set_default_navigation_timeout = MagicMock()
        ctx.add_init_script = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=ctx)

        fetcher = GrokFetcher(state_path=tmp_path / "state.json")
        fetcher._browser = browser

        result = await fetcher.fetch("https://grok.com/chat/abc")
        assert result.success is False
        assert result.failure_reason == "http_error_403"

    @pytest.mark.asyncio
    async def test_fetch_handles_timeout(self, tmp_path: Path) -> None:
        from playwright.async_api import TimeoutError as PWTimeout

        page = MagicMock()
        page.goto = AsyncMock(side_effect=PWTimeout("timeout"))
        page.wait_for_timeout = AsyncMock()
        page.url = "https://grok.com/share/slow"

        ctx = MagicMock()
        ctx.set_default_navigation_timeout = MagicMock()
        ctx.add_init_script = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=ctx)

        fetcher = GrokFetcher(state_path=tmp_path / "state.json")
        fetcher._browser = browser

        result = await fetcher.fetch("https://grok.com/share/slow")
        assert result.success is False
        assert result.failure_reason is not None
        assert "timeout" in result.failure_reason.lower()

    @pytest.mark.asyncio
    async def test_has_session_reflects_state_path(self, tmp_path: Path) -> None:
        sp = tmp_path / "state.json"
        fetcher = GrokFetcher(state_path=sp)
        assert not await fetcher.has_session()
        sp.write_text("{}", encoding="utf-8")
        assert await fetcher.has_session()

    @pytest.mark.asyncio
    async def test_uses_storage_state_when_present(self, tmp_path: Path) -> None:
        sp = tmp_path / "state.json"
        sp.write_text("{}", encoding="utf-8")

        page = MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(status=200))
        page.wait_for_timeout = AsyncMock()
        page.content = AsyncMock(return_value="<html>ok</html>")
        page.title = AsyncMock(return_value="ok")
        page.url = "https://grok.com/share/x"

        ctx = MagicMock()
        ctx.set_default_navigation_timeout = MagicMock()
        ctx.add_init_script = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=ctx)

        fetcher = GrokFetcher(state_path=sp)
        fetcher._browser = browser

        await fetcher.fetch("https://grok.com/share/x")
        # new_context への kwargs に storage_state が含まれている
        call_kwargs = browser.new_context.call_args.kwargs
        assert "storage_state" in call_kwargs
        assert str(sp) == call_kwargs["storage_state"]
