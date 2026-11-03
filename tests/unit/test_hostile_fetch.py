"""敵対的 host への取得ポリシー (hostile_fetch) のテスト。実際には sleep しない。"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest

from src.tools import hostile_fetch as hf
from src.tools.fetch_policy import FEED_ACCEPT, staged_get, staged_get_sync
from src.tools.user_agent import (
    NEUTRAL_BOT_UA,
    bot_user_agent,
    resolve_bot_identity,
)

# 毎時の境界から十分離れた固定時刻 (offset 1800 秒)。
MID_HOUR = 3_600_000.0 + 1800.0


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        hf.ENV_POLICY,
        hf.ENV_PAUSE,
        hf.ENV_MIN_INTERVAL,
        hf.ENV_MAX_WAIT,
        hf.ENV_GEOIP_DB,
        "BOT_UA_NEUTRAL",
    ):
        monkeypatch.delenv(name, raising=False)
    hf.clear_tagged_hosts()
    monkeypatch.setattr(hf, "_THROTTLE", hf.HostThrottle(clock=lambda: MID_HOUR))
    yield
    hf.clear_tagged_hosts()


class TestModes:
    def test_default_is_shadow(self) -> None:
        assert hf.policy_mode() == "shadow"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("0", "off"), ("1", "on"), ("shadow", "shadow"), ("garbage", "shadow")],
    )
    def test_policy_mode_parsing(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
    ) -> None:
        monkeypatch.setenv(hf.ENV_POLICY, raw)
        assert hf.policy_mode() == expected

    def test_pause_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert not hf.is_paused()
        monkeypatch.setenv(hf.ENV_PAUSE, "1")
        assert hf.is_paused()


class TestAttachments:
    @pytest.mark.parametrize(
        "url",
        [
            "https://a.cn/report.PDF",
            "https://a.cn/x/doc.docx?dl=1",
            "https://a.cn/sheet.xlsx",
            "https://a.cn/pack.zip",
        ],
    )
    def test_attachment_suffix_detected(self, url: str) -> None:
        assert hf.is_attachment_url(url)

    @pytest.mark.parametrize("url", ["https://a.cn/news/1", "https://a.cn/sitemap.xml.gz"])
    def test_html_and_sitemap_gz_are_not_attachments(self, url: str) -> None:
        assert not hf.is_attachment_url(url)

    @pytest.mark.parametrize(
        "headers",
        [
            {"content-type": "application/pdf"},
            {"content-type": "application/octet-stream"},
            {"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml"},
            {"content-type": "text/html", "content-disposition": "attachment; filename=a.bin"},
        ],
    )
    def test_attachment_response_detected(self, headers: dict[str, str]) -> None:
        assert hf.is_attachment_response(headers)

    @pytest.mark.parametrize(
        "headers",
        [
            {"content-type": "text/html; charset=utf-8"},
            {"content-type": "application/rss+xml"},
            {},
        ],
    )
    def test_text_response_is_not_attachment(self, headers: dict[str, str]) -> None:
        assert not hf.is_attachment_response(headers)


class TestIntervalMath:
    def test_avoid_hour_boundary_shifts_points_near_the_hour(self) -> None:
        assert hf.avoid_hour_boundary(7200.0 + 5) == 7200.0 + hf.HOUR_AVOID_WINDOW_SECONDS
        assert hf.avoid_hour_boundary(7200.0 - 5) == 7200.0 + hf.HOUR_AVOID_WINDOW_SECONDS
        assert hf.avoid_hour_boundary(MID_HOUR) == MID_HOUR

    def test_next_slot_honours_interval_jitter_and_not_before(self) -> None:
        now = MID_HOUR
        assert hf.next_slot(now=now, last_slot=None, not_before=0, interval=120, jitter=30) == now
        assert (
            hf.next_slot(now=now, last_slot=now, not_before=0, interval=120, jitter=30) == now + 150
        )
        assert (
            hf.next_slot(now=now, last_slot=now, not_before=now + 600, interval=120, jitter=0)
            == now + 600
        )

    def test_parse_retry_after_seconds_and_date(self) -> None:
        now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
        assert hf.parse_retry_after("90") == 90.0
        assert hf.parse_retry_after("Fri, 30 Oct 2026 12:05:00 GMT", now=now) == 300.0
        assert hf.parse_retry_after("nonsense") is None
        assert hf.parse_retry_after("999999") == hf.MAX_RETRY_AFTER_SECONDS


class TestThrottle:
    def _throttle(self, clock: list[float]) -> hf.HostThrottle:
        return hf.HostThrottle(clock=lambda: clock[0], rng=random.Random(1))

    def test_first_call_is_immediate_then_spaced_by_interval(self) -> None:
        clock = [MID_HOUR]
        t = self._throttle(clock)
        assert t.reserve("a.cn") == 0.0
        second = t.reserve("a.cn")
        assert second is not None
        assert hf.DEFAULT_MIN_INTERVAL_SECONDS <= second <= hf.DEFAULT_MIN_INTERVAL_SECONDS * 1.5

    def test_hosts_are_independent(self) -> None:
        t = self._throttle([MID_HOUR])
        t.reserve("a.cn")
        assert t.reserve("b.ru") == 0.0

    def test_backlog_beyond_max_wait_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(hf.ENV_MAX_WAIT, "200")
        t = self._throttle([MID_HOUR])
        t.reserve("a.cn")
        t.reserve("a.cn")  # ~120-180 秒先
        assert t.reserve("a.cn") is None  # さらに先は上限 200 秒を超える

    def test_retry_after_pushes_next_slot(self) -> None:
        clock = [MID_HOUR]
        t = self._throttle(clock)
        t.note_retry_after("a.cn", 900)
        assert t.reserve("a.cn") is None  # 900 秒待ちは上限 (既定 300) 超過
        clock[0] = MID_HOUR + 900
        assert t.reserve("a.cn") == 0.0

    def test_interval_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(hf.ENV_MIN_INTERVAL, "10")
        t = self._throttle([MID_HOUR])
        t.reserve("a.cn")
        second = t.reserve("a.cn")
        assert second is not None
        assert 10 <= second <= 15


class TestUserAgentNeutrality:
    def test_neutral_ua_has_no_project_name_url_or_role(self) -> None:
        for role in ("rss-fetcher", "sitemap-watcher", "html-listing-watcher", "source-discovery"):
            ua = bot_user_agent(role)
            assert ua == NEUTRAL_BOT_UA
            for forbidden in ("kuebiko", "http", "github", "@", role):
                assert forbidden not in ua.lower()

    def test_flag_zero_restores_legacy_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_UA_NEUTRAL", "0")
        assert bot_user_agent("rss-fetcher") == "kuebiko/1.0 (+rss-fetcher)"
        assert resolve_bot_identity("kuebiko/1.0 (+x)") == "kuebiko/1.0 (+x)"

    def test_legacy_identity_is_rewritten_when_neutral(self) -> None:
        assert resolve_bot_identity("kuebiko/1.0 (+rss-fetcher)") == NEUTRAL_BOT_UA
        assert resolve_bot_identity("custom/2.0") == "custom/2.0"

    def test_no_module_ships_the_old_string(self) -> None:
        from pathlib import Path

        for rel in (
            "src/tools/direct_rss_source.py",
            "src/watchers/sitemap_base.py",
            "src/watchers/html_listing.py",
            "src/ui/api/_source_http.py",
            "src/sources/ransomware_live.py",
        ):
            text = (Path(__file__).resolve().parents[2] / rel).read_text(encoding="utf-8")
            # BOT_UA_NEUTRAL=0 の戻し先 (中立化前の値) の定数 1 行だけは許す
            text = "\n".join(
                line for line in text.splitlines() if not line.startswith("_LEGACY_USER_AGENT")
            )
            assert "kuebiko/1.0" not in text, rel
            assert '"kuebiko-bot"' not in text, rel


def _recording_transport(log: list[httpx.Request], routes: dict[str, httpx.Response]):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        return routes.get(str(request.url), httpx.Response(200, text="<rss/>"))

    return httpx.MockTransport(handler)


class TestStagedGetShadow:
    """shadow (既定): 分類はするが挙動は不変。"""

    @pytest.mark.asyncio
    async def test_hostile_host_still_gets_bot_ua_in_shadow(self) -> None:
        log: list[httpx.Request] = []
        async with httpx.AsyncClient(transport=_recording_transport(log, {})) as c:
            _resp, stage = await staged_get(
                c, "https://news.example.ru/feed", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
            )
        assert stage == "bot"
        assert log[0].headers["user-agent"] == NEUTRAL_BOT_UA

    @pytest.mark.asyncio
    async def test_shadow_logs_classification(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("INFO")
        async with httpx.AsyncClient(transport=_recording_transport([], {})) as c:
            await staged_get(
                c, "https://news.example.ru/feed", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
            )
        assert "hostile_host_policy" in caplog.text

    @pytest.mark.asyncio
    async def test_pause_blocks_hostile_even_in_shadow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(hf.ENV_PAUSE, "1")
        log: list[httpx.Request] = []
        async with httpx.AsyncClient(transport=_recording_transport(log, {})) as c:
            with pytest.raises(hf.HostileFetchSkipped) as ei:
                await staged_get(
                    c, "https://news.example.ru/feed", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
                )
        assert ei.value.reason == "paused"
        assert log == []

    @pytest.mark.asyncio
    async def test_pause_does_not_affect_ordinary_hosts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(hf.ENV_PAUSE, "1")
        log: list[httpx.Request] = []
        async with httpx.AsyncClient(transport=_recording_transport(log, {})) as c:
            await staged_get(
                c, "https://www.cisa.gov/feed", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
            )
        assert len(log) == 1


class TestStagedGetEnforced:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(hf.ENV_POLICY, "1")

    @pytest.mark.asyncio
    async def test_hostile_host_gets_browser_ua_only_and_no_escalation(self) -> None:
        log: list[httpx.Request] = []
        routes = {"https://news.example.ru/feed": httpx.Response(403)}
        async with httpx.AsyncClient(transport=_recording_transport(log, routes)) as c:
            resp, stage = await staged_get(
                c, "https://news.example.ru/feed", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
            )
        assert resp.status_code == 403
        assert stage == "browser"
        assert len(log) == 1  # エスカレーションの再試行をしない
        assert NEUTRAL_BOT_UA not in log[0].headers["user-agent"]
        assert "Mozilla" in log[0].headers["user-agent"]

    @pytest.mark.asyncio
    async def test_ordinary_host_keeps_bot_then_escalates(self) -> None:
        log: list[httpx.Request] = []
        routes = {"https://www.cisa.gov/feed": httpx.Response(403)}
        async with httpx.AsyncClient(transport=_recording_transport(log, routes)) as c:
            await staged_get(
                c, "https://www.cisa.gov/feed", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
            )
        assert log[0].headers["user-agent"] == NEUTRAL_BOT_UA
        assert len(log) == 2
        assert "Mozilla" in log[1].headers["user-agent"]

    @pytest.mark.asyncio
    async def test_source_tag_makes_ordinary_host_hostile(self) -> None:
        log: list[httpx.Request] = []
        async with httpx.AsyncClient(transport=_recording_transport(log, {})) as c:
            _resp, stage = await staged_get(
                c,
                "https://www.cisa.gov/feed",
                identity=NEUTRAL_BOT_UA,
                accept=FEED_ACCEPT,
                source_profile="hostile",
            )
        assert stage == "browser"
        assert "Mozilla" in log[0].headers["user-agent"]

    @pytest.mark.asyncio
    async def test_tag_is_inherited_by_article_urls_on_the_same_host(self) -> None:
        async with httpx.AsyncClient(transport=_recording_transport([], {})) as c:
            await staged_get(
                c,
                "https://feeds.example.org/rss",
                identity=NEUTRAL_BOT_UA,
                accept=FEED_ACCEPT,
                source_profile="hostile",
            )
        assert hf.plan_for("https://feeds.example.org/post/1").hostile

    @pytest.mark.asyncio
    async def test_redirect_from_ordinary_host_into_hostile_is_rechecked(self) -> None:
        log: list[httpx.Request] = []
        routes = {
            "https://www.cisa.gov/old": httpx.Response(
                301, headers={"location": "https://mirror.example.ru/new"}
            ),
        }
        async with httpx.AsyncClient(transport=_recording_transport(log, routes)) as c:
            resp, stage = await staged_get(
                c, "https://www.cisa.gov/old", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
            )
        assert resp.status_code == 200
        assert stage == "browser"  # 最終ホップが hostile
        assert [r.url.host for r in log] == ["www.cisa.gov", "mirror.example.ru"]
        assert log[0].headers["user-agent"] == NEUTRAL_BOT_UA  # 最初のホップは通常どおり
        assert "Mozilla" in log[1].headers["user-agent"]  # hostile ホップはブラウザ UA のみ

    @pytest.mark.asyncio
    async def test_redirect_into_hostile_attachment_is_refused(self) -> None:
        log: list[httpx.Request] = []
        routes = {
            "https://www.cisa.gov/dl": httpx.Response(
                302, headers={"location": "https://files.example.cn/report.pdf"}
            ),
        }
        async with httpx.AsyncClient(transport=_recording_transport(log, routes)) as c:
            with pytest.raises(hf.HostileFetchSkipped) as ei:
                await staged_get(
                    c, "https://www.cisa.gov/dl", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
                )
        assert ei.value.reason == "attachment_url"
        assert [r.url.host for r in log] == ["www.cisa.gov"]  # 添付先には接続しない

    @pytest.mark.asyncio
    async def test_attachment_content_type_from_hostile_host_is_refused(self) -> None:
        routes = {
            "https://news.example.cn/get": httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7"
            ),
        }
        async with httpx.AsyncClient(transport=_recording_transport([], routes)) as c:
            with pytest.raises(hf.HostileFetchSkipped) as ei:
                await staged_get(
                    c, "https://news.example.cn/get", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
                )
        assert ei.value.reason == "attachment_content_type"

    @pytest.mark.asyncio
    async def test_paused_hostile_is_not_contacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(hf.ENV_PAUSE, "1")
        log: list[httpx.Request] = []
        async with httpx.AsyncClient(transport=_recording_transport(log, {})) as c:
            with pytest.raises(hf.HostileFetchSkipped):
                await staged_get(
                    c, "https://news.example.ru/feed", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
                )
        assert log == []

    @pytest.mark.asyncio
    async def test_second_request_to_same_hostile_host_waits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        waits: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            waits.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        async with httpx.AsyncClient(transport=_recording_transport([], {})) as c:
            for path in ("a", "b"):
                await staged_get(
                    c,
                    f"https://news.example.ru/{path}",
                    identity=NEUTRAL_BOT_UA,
                    accept=FEED_ACCEPT,
                )
        assert len(waits) == 1
        assert waits[0] >= hf.DEFAULT_MIN_INTERVAL_SECONDS

    def test_sync_path_applies_the_same_policy(self) -> None:
        log: list[httpx.Request] = []
        with httpx.Client(transport=_recording_transport(log, {})) as c:
            _resp, stage = staged_get_sync(
                c, "https://news.example.ru/feed", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
            )
        assert stage == "browser"
        assert "Mozilla" in log[0].headers["user-agent"]

    @pytest.mark.asyncio
    async def test_retry_after_is_recorded_for_hostile_host(self) -> None:
        routes = {
            "https://news.example.ru/x": httpx.Response(429, headers={"retry-after": "250"}),
        }
        async with httpx.AsyncClient(transport=_recording_transport([], routes)) as c:
            await staged_get(
                c, "https://news.example.ru/x", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
            )
        # 次の予約は 250 秒後以降 (上限 300 秒以内)
        delay = hf.default_throttle().reserve("news.example.ru")
        assert delay is not None
        assert delay >= 250


class TestPlaywrightGuard:
    def test_skipped_for_hostile_when_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(hf.ENV_POLICY, "1")
        assert hf.should_skip_playwright("https://news.example.ru/a")
        assert not hf.should_skip_playwright("https://www.cisa.gov/a")

    def test_not_skipped_in_shadow_but_skipped_when_paused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert not hf.should_skip_playwright("https://news.example.ru/a")
        monkeypatch.setenv(hf.ENV_PAUSE, "1")
        assert hf.should_skip_playwright("https://news.example.ru/a")

    @pytest.mark.asyncio
    async def test_extractor_does_not_launch_playwright_for_hostile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.tools.content_extractor import ContentExtractor

        monkeypatch.setenv(hf.ENV_POLICY, "1")
        ex = ContentExtractor(client=httpx.AsyncClient(transport=_recording_transport([], {})))
        called: list[str] = []

        async def boom(url: str) -> None:
            called.append(url)

        monkeypatch.setattr(ex, "_extract_with_playwright", boom)
        assert await ex._playwright_within_cap("https://news.example.ru/a") is None
        assert called == []
        await ex._playwright_within_cap("https://www.cisa.gov/a")
        assert called == ["https://www.cisa.gov/a"]

    @pytest.mark.asyncio
    async def test_listing_playwright_is_skipped_for_hostile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.tools.playwright_fetch import fetch_text_via_playwright

        monkeypatch.setenv(hf.ENV_POLICY, "1")
        assert await fetch_text_via_playwright("https://news.example.ru/a", user_agent="x") is None


class TestExtractorEnforced:
    @pytest.mark.asyncio
    async def test_hostile_attachment_url_fails_with_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.tools.content_extractor import ContentExtractor

        monkeypatch.setenv(hf.ENV_POLICY, "1")
        monkeypatch.setattr("src.tools.content_extractor.assert_safe_public_url", lambda _u: None)
        log: list[httpx.Request] = []
        ex = ContentExtractor(client=httpx.AsyncClient(transport=_recording_transport(log, {})))
        result = await ex.extract("https://files.example.cn/report.pdf")
        assert not result.success
        assert result.failure_reason == "hostile_attachment_url"
        assert log == []

    @pytest.mark.asyncio
    async def test_paused_hostile_fails_without_connecting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.tools.content_extractor import ContentExtractor

        monkeypatch.setenv(hf.ENV_PAUSE, "1")
        monkeypatch.setattr("src.tools.content_extractor.assert_safe_public_url", lambda _u: None)
        log: list[httpx.Request] = []
        ex = ContentExtractor(client=httpx.AsyncClient(transport=_recording_transport(log, {})))
        result = await ex.extract("https://news.example.ru/a")
        assert result.failure_reason == "hostile_paused"
        assert log == []
