"""fetch_policy (段階的エスカレーション SSoT) のテスト (2026-08-01)。

核心の不変量:
- ブロック署名 status のときだけブラウザ UA へ 1 段エスカレーションする
- ネットワーク例外ではエスカレーションしない (bot 段は素通し / browser 段は best-effort)
- 最終失敗時は「本来の (bot 段の) ブロック status」を報告する
"""

from __future__ import annotations

import httpx
import pytest

from src.tools.fetch_policy import (
    BLOCK_ESCALATION_STATUSES,
    FEED_ACCEPT,
    bot_headers,
    escalated_browser_headers,
    should_escalate,
    staged_get,
    staged_get_sync,
)

BOT_IDENTITY = "kuebiko/1.0 (+test)"


class TestPolicy:
    @pytest.mark.parametrize("status", sorted(BLOCK_ESCALATION_STATUSES))
    def test_block_statuses_escalate(self, status: int) -> None:
        assert should_escalate(status) is True

    @pytest.mark.parametrize("status", [200, 301, 304, 400, 404, 410, 500, 502, 520])
    def test_non_block_statuses_do_not_escalate(self, status: int) -> None:
        # 404/5xx はコンテンツ/サーバ側の問題で UA では変わらない
        assert should_escalate(status) is False

    def test_bot_headers_carry_identity_and_accept(self) -> None:
        h = bot_headers(BOT_IDENTITY, accept=FEED_ACCEPT)
        assert h["User-Agent"] == BOT_IDENTITY
        assert "*/*" in h["Accept"]  # RFC 7231: 必ず fallback 付き

    def test_browser_headers_resolve_ua_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # UA 自己修復ジョブが env を更新したら、再起動なしで新 UA を拾う
        monkeypatch.setenv("CONTENT_EXTRACTOR_USER_AGENT", "TestBrowser/999")
        h = escalated_browser_headers()
        assert h["User-Agent"] == "TestBrowser/999"
        assert "Accept-Language" in h


def _transport(
    handler_log: list[str], *, bot_status: int, browser_status: int
) -> httpx.MockTransport:
    """UA で応答を分岐する transport。呼ばれた UA を handler_log に記録する。"""

    def handler(request: httpx.Request) -> httpx.Response:
        ua = request.headers.get("User-Agent", "")
        handler_log.append(ua)
        if ua == BOT_IDENTITY:
            return httpx.Response(bot_status, text=f"bot:{bot_status}")
        return httpx.Response(browser_status, text=f"browser:{browser_status}")

    return httpx.MockTransport(handler)


class TestStagedGetSync:
    def test_bot_success_does_not_escalate(self) -> None:
        log: list[str] = []
        with httpx.Client(transport=_transport(log, bot_status=200, browser_status=200)) as c:
            resp, stage = staged_get_sync(
                c, "https://x.test/feed", identity=BOT_IDENTITY, accept=FEED_ACCEPT
            )
        assert (resp.status_code, stage) == (200, "bot")
        assert log == [BOT_IDENTITY]  # 1 回しか呼ばない

    def test_blocked_bot_escalates_to_browser(self) -> None:
        log: list[str] = []
        with httpx.Client(transport=_transport(log, bot_status=403, browser_status=200)) as c:
            resp, stage = staged_get_sync(
                c, "https://x.test/feed", identity=BOT_IDENTITY, accept=FEED_ACCEPT
            )
        assert (resp.status_code, stage) == (200, "browser")
        assert len(log) == 2
        assert log[1] != BOT_IDENTITY  # 2 回目はブラウザ UA

    def test_both_blocked_reports_original_bot_status(self) -> None:
        log: list[str] = []
        with httpx.Client(transport=_transport(log, bot_status=403, browser_status=403)) as c:
            resp, stage = staged_get_sync(
                c, "https://x.test/feed", identity=BOT_IDENTITY, accept=FEED_ACCEPT
            )
        # 記録されるエラーは「本来のブロック status」= bot 段の応答
        assert (resp.status_code, stage) == (403, "bot")

    def test_non_block_error_does_not_escalate(self) -> None:
        log: list[str] = []
        with httpx.Client(transport=_transport(log, bot_status=404, browser_status=200)) as c:
            resp, stage = staged_get_sync(
                c, "https://x.test/feed", identity=BOT_IDENTITY, accept=FEED_ACCEPT
            )
        assert (resp.status_code, stage) == (404, "bot")
        assert log == [BOT_IDENTITY]

    def test_browser_stage_network_error_falls_back_to_bot_response(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            ua = request.headers.get("User-Agent", "")
            calls.append(ua)
            if ua == BOT_IDENTITY:
                return httpx.Response(403, text="blocked")
            raise httpx.ConnectError("boom")

        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            resp, stage = staged_get_sync(
                c, "https://x.test/feed", identity=BOT_IDENTITY, accept=FEED_ACCEPT
            )
        assert (resp.status_code, stage) == (403, "bot")
        assert len(calls) == 2

    def test_bot_stage_network_error_propagates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("tarpit")

        with (
            httpx.Client(transport=httpx.MockTransport(handler)) as c,
            pytest.raises(httpx.ConnectTimeout),
        ):
            staged_get_sync(c, "https://x.test/feed", identity=BOT_IDENTITY, accept=FEED_ACCEPT)


@pytest.mark.asyncio
class TestStagedGetAsync:
    async def test_blocked_bot_escalates_to_browser(self) -> None:
        log: list[str] = []
        async with httpx.AsyncClient(
            transport=_transport(log, bot_status=403, browser_status=200)
        ) as c:
            resp, stage = await staged_get(
                c, "https://x.test/feed", identity=BOT_IDENTITY, accept=FEED_ACCEPT
            )
        assert (resp.status_code, stage) == (200, "browser")

    async def test_bot_success_single_request(self) -> None:
        log: list[str] = []
        async with httpx.AsyncClient(
            transport=_transport(log, bot_status=200, browser_status=200)
        ) as c:
            resp, stage = await staged_get(
                c, "https://x.test/feed", identity=BOT_IDENTITY, accept=FEED_ACCEPT
            )
        assert (resp.status_code, stage) == (200, "bot")
        assert log == [BOT_IDENTITY]


class TestJsChallengeFingerprint:
    """JS チャレンジ指紋 (Playwright 第 3 段の発火判断、status 非依存)。"""

    @pytest.mark.parametrize(
        "body",
        [
            # 実測 (2026-08-01 ipdefenseforum、307 応答の本文)
            "<html><title>You are being redirected...</title>"
            "<noscript>Javascript is required. Please enable javascript"
            "</noscript><script>var s={}</script></html>",
            "<html><head><title>Just a moment...</title></head></html>",
            "checking your browser before accessing example.com",
            "<script src='/cdn-cgi/challenge-platform/x.js'></script>",
        ],
    )
    def test_challenge_pages_are_detected(self, body: str) -> None:
        from src.tools.fetch_policy import looks_like_js_challenge

        assert looks_like_js_challenge(body) is True

    @pytest.mark.parametrize(
        "body",
        [
            "<html><body><h1>APT41 targets telecom sector</h1><p>full article</p></body></html>",
            '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>',
            "",
            # 記事本文に "javascript" が普通に出るだけでは発火しない
            "<p>The malware uses javascript obfuscation techniques.</p>",
        ],
    )
    def test_normal_content_is_not_detected(self, body: str) -> None:
        from src.tools.fetch_policy import looks_like_js_challenge

        assert looks_like_js_challenge(body) is False

    def test_fingerprint_only_scanned_in_head(self) -> None:
        from src.tools.fetch_policy import looks_like_js_challenge

        # 先頭 2000 字より後にだけ出る文言は照合しない (巨大記事の偶然一致を防ぐ)
        body = ("x" * 3000) + "javascript is required"
        assert looks_like_js_challenge(body) is False
