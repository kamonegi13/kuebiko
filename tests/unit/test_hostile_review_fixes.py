"""レビュー指摘 (MEDIUM1-4 / LOW) の回帰テスト。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from playwright.async_api import Browser

from src.synthesis.grounded.detect_features import FEATURE_NAMES, DetectArticle, feature_vector
from src.tools import hostile_fetch as hf
from src.tools.fetch_policy import FEED_ACCEPT, staged_get
from src.tools.user_agent import NEUTRAL_BOT_UA

MID_HOUR = 3_600_000.0 + 1800.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (hf.ENV_POLICY, hf.ENV_PAUSE, hf.ENV_INLINE_WAIT, hf.ENV_GEOIP_DB):
        monkeypatch.delenv(name, raising=False)
    hf.clear_tagged_hosts()
    monkeypatch.setattr(hf, "_THROTTLE", hf.HostThrottle(clock=lambda: MID_HOUR))
    yield
    hf.clear_tagged_hosts()


class TestTaggedHostPollution:
    def test_generic_hosts_linked_from_a_hostile_feed_are_not_tagged(self) -> None:
        # Arrange / Act
        github = hf.register_tagged_url(
            "https://github.com/o/r", anchor_url="https://feeds.example.org/rss"
        )
        own = hf.register_tagged_url(
            "https://www.example.org/a/1", anchor_url="https://feeds.example.org/rss"
        )
        # Assert
        assert not github
        assert own
        assert not hf.plan_for("https://github.com/x").hostile
        assert hf.plan_for("https://www.example.org/b").hostile

    def test_second_level_suffix_is_one_registered_domain(self) -> None:
        assert hf.registered_domain("news.example.co.uk") == "example.co.uk"
        assert hf.registered_domain("a.b.example.com") == "example.com"

    def test_tag_expires_after_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [1000.0]
        monkeypatch.setattr(hf, "_tag_clock", lambda: now[0])
        hf.register_tagged_url("https://feeds.example.org/rss")
        assert hf.plan_for("https://feeds.example.org/a").hostile
        now[0] += hf.TAG_TTL_SECONDS + 1
        assert not hf.plan_for("https://feeds.example.org/a").hostile


class TestMlFeatureMapping:
    def test_adversary_official_tier_uses_state_media_column(self) -> None:
        def vec(tier: str) -> list[float]:
            a = DetectArticle(
                article_id="x", title="t", summary="s", importance="medium",
                category="breach", tier=tier, kind="breach",
            )  # fmt: skip
            return feature_vector(a)

        adversary = dict(zip(FEATURE_NAMES, vec("adversary_official"), strict=True))
        assert adversary["tier=state_media"] == 1.0
        assert vec("adversary_official") == vec("state_media")


class TestShadowIsBehaviourPreserving:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["https://news.example.ru/feed", "https://www.cisa.gov/feed"])
    async def test_shadow_requests_equal_policy_off(
        self, monkeypatch: pytest.MonkeyPatch, path: str
    ) -> None:
        async def run(mode: str) -> list[tuple[str, str]]:
            monkeypatch.setenv(hf.ENV_POLICY, mode)
            seen: list[tuple[str, str]] = []

            def handler(req: httpx.Request) -> httpx.Response:
                seen.append((str(req.url), req.headers["user-agent"]))
                return httpx.Response(403 if len(seen) == 1 else 200, text="<rss/>")

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                await staged_get(c, path, identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT)
            return seen

        assert await run("shadow") == await run("0")


class TestExtractorHostile:
    @pytest.mark.asyncio
    async def test_ua_rotation_stops_after_first_block_for_hostile_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.tools.content_extractor import ContentExtractor

        monkeypatch.setenv(hf.ENV_POLICY, "1")
        monkeypatch.setattr("src.tools.content_extractor.assert_safe_public_url", lambda _u: None)
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req.headers["user-agent"])
            return httpx.Response(403, text="blocked")

        ex = ContentExtractor(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        result = await ex.extract("https://news.example.ru/a")
        assert result.failure_reason == "http_error_403"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_ordinary_host_still_rotates_user_agents(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.tools.content_extractor import ContentExtractor

        monkeypatch.setenv(hf.ENV_POLICY, "1")
        monkeypatch.setattr("src.tools.content_extractor.assert_safe_public_url", lambda _u: None)
        calls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req.headers["user-agent"])
            return httpx.Response(403, text="blocked")

        ex = ContentExtractor(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            enable_playwright_fallback=False,
        )
        await ex.extract("https://www.cisa.gov/a")
        assert len(calls) > 1

    @pytest.mark.asyncio
    async def test_second_article_on_same_host_is_deferred_not_waited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.tools.content_extractor import ContentExtractor

        monkeypatch.setenv(hf.ENV_POLICY, "1")
        monkeypatch.setattr("src.tools.content_extractor.assert_safe_public_url", lambda _u: None)
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html><body>x</body></html>")

        ex = ContentExtractor(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            enable_playwright_fallback=False,
        )
        await ex.extract("https://news.example.ru/1")
        second = await ex.extract("https://news.example.ru/2")
        assert second.failure_reason == "hostile_throttle_deferred"
        assert slept == []  # スロットを握って待たない


class TestPlaywrightPathsGuarded:
    @pytest.mark.asyncio
    async def test_playwright_watcher_skips_hostile_target(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from src.watchers.playwright_base import PlaywrightWatcher

        monkeypatch.setenv(hf.ENV_POLICY, "1")
        w = PlaywrightWatcher(
            name="t",
            target_url="https://news.example.ru/",
            link_selector="a",
            url_include_pattern=re.compile(r"."),
            seen_file=tmp_path / "seen.json",
            state_root=tmp_path,
            feed_title="t",
        )
        assert await w._collect_all_urls() == []

    @pytest.mark.asyncio
    async def test_grok_fetcher_refuses_hostile_url(self) -> None:
        from src.grok.fetcher import GrokFetcher, GrokFetchError

        fetcher = object.__new__(GrokFetcher)
        fetcher._browser = cast(Browser, object())
        with pytest.raises(GrokFetchError):
            await fetcher._fetch_locked("https://news.example.ru/x")


class TestGeoipDoesNotBlockLoop:
    @pytest.mark.asyncio
    async def test_aplan_uses_thread_only_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        threaded: list[str] = []

        async def fake_to_thread(fn, *a, **kw):  # type: ignore[no-untyped-def]
            threaded.append(fn.__name__)
            return fn(*a, **kw)

        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
        await hf.aplan_for("https://www.cisa.gov/")
        assert threaded == []
        monkeypatch.setenv(hf.ENV_GEOIP_DB, "/nonexistent.mmdb")
        await hf.aplan_for("https://www.cisa.gov/")
        assert threaded == ["plan_for"]


class TestClassifierFailuresNeverBreakFetching:
    @pytest.mark.parametrize("content", ["- a\n- b\n", "just a string", "cctlds: ru\ndomains: x\n"])
    def test_invalid_config_structure_falls_back_to_builtin_cctlds(
        self, tmp_path: Path, content: str
    ) -> None:
        from src.tools.hostile_hosts import load_lists

        path = tmp_path / "bad.yaml"
        path.write_text(content, encoding="utf-8")
        lists = load_lists(path)
        assert "ru" in lists.cctlds
        assert lists.domains == frozenset()

    def test_empty_config_uses_builtin_cctlds(self, tmp_path: Path) -> None:
        from src.tools.hostile_hosts import load_lists

        path = tmp_path / "empty.yaml"
        path.write_text("", encoding="utf-8")
        assert "cn" in load_lists(path).cctlds

    def test_plan_for_treats_classifier_exception_as_hostile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(hf, "classify_host", boom)
        plan = hf.plan_for("https://www.cisa.gov/x")
        assert plan.hostile
        assert plan.verdict.reasons == ("classifier_error",)

    @pytest.mark.asyncio
    async def test_shadow_fetch_proceeds_when_classifier_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(hf, "classify_host", boom)
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.headers["user-agent"])
            return httpx.Response(200, text="<rss/>")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            resp, stage = await staged_get(
                c, "https://www.cisa.gov/feed", identity=NEUTRAL_BOT_UA, accept=FEED_ACCEPT
            )
        assert resp.status_code == 200
        assert stage == "bot"
        assert seen == [NEUTRAL_BOT_UA]  # shadow: 従来どおりのボット UA で 1 回

    @pytest.mark.asyncio
    async def test_extractor_get_survives_logging_and_classifier_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.tools.content_extractor import ContentExtractor

        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(hf, "classify_host", boom)
        monkeypatch.setattr(hf._log, "info", boom)
        ex = ContentExtractor(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="ok"))
            )
        )
        resp = await ex._get("https://www.cisa.gov/a")
        assert resp.status_code == 200

    def test_observe_redirect_history_swallows_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(hf, "plan_for", boom)
        monkeypatch.setattr(hf, "policy_mode", lambda: "shadow")
        req = httpx.Request("GET", "https://a.example.com/")
        first = httpx.Response(301, request=req)
        final = httpx.Response(200, request=httpx.Request("GET", "https://b.example.com/"))
        final.history = [first]
        hf.observe_redirect_history(final)  # 例外を出さない

    def test_untrusted_framing_check_survives_classifier_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.cti import adversary_claims as ac

        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(ac, "classify_host", boom)
        assert ac.needs_untrusted_framing("t", "https://www.cisa.gov/f", "https://www.cisa.gov/a")


class TestNeutralTokenAndRefetchOrder:
    def test_neutral_token_does_not_resemble_a_known_crawler(self) -> None:
        for crawler in ("feedfetcher", "googlebot", "bingbot", "feedly", "bot"):
            assert crawler not in NEUTRAL_BOT_UA.lower()
        assert "kuebiko" not in NEUTRAL_BOT_UA.lower()

    def test_deferred_articles_go_last_and_expire(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime, timedelta

        from src.storage.records import ArticleRecord, RunRecord
        from src.storage.run_history import RunHistoryRepository

        repo = RunHistoryRepository(tmp_path / "rh.db")
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False)
        )
        for aid in ("d:deferred", "d:normal"):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=aid,
                    title="t",
                    url=f"https://e.example.com/{aid}",
                    feed_title="f",
                    status="posted",
                    importance="high" if aid == "d:deferred" else "low",
                )
            )
        repo.record_refetch_failure("d:deferred", reason="hostile_throttle_deferred")
        ids = [a for a, _ in repo.list_articles_needing_refetch()]
        assert ids == ["d:normal", "d:deferred"]  # high でも見送り済みは末尾

        # 作成から deferred_max_age_days を過ぎた見送りは対象から外れる
        with repo._connect() as conn:  # noqa: SLF001
            old = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE articles SET created_at=? WHERE article_id='d:deferred'", (old,))
        ids2 = [a for a, _ in repo.list_articles_needing_refetch()]
        assert "d:deferred" not in ids2
