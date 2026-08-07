"""src.tools.source_router のテスト (Phase 2)。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config_loader import SourceConfig
from src.grok.fetcher import GrokFetchResult
from src.tools.imap_client import EmailMessage
from src.tools.source_router import (
    GrokEmailSource,
    WebScraperClusterSource,
    _stable_article_id,
    build_source,
)


def _email(uid: str = "1", urls: list[str] | None = None) -> EmailMessage:
    return EmailMessage(
        uid=uid,
        subject="Grok report",
        sender="noreply@x.ai",
        received_at=datetime(2024, 1, 1, tzinfo=UTC),
        body_text="visit url",
        body_html="<a href='x'>x</a>",
        extracted_urls=urls or ["https://grok.com/share/abc"],
    )


# ---------- helpers ----------


class TestStableArticleId:
    def test_same_url_same_id(self) -> None:
        a = _stable_article_id("https://x.com/", "1")
        b = _stable_article_id("https://x.com/", "2")
        # 同じ URL でも fallback が違うので末尾が違う
        assert a.startswith("grok:")
        assert b.startswith("grok:")
        assert a.split(":")[1] == b.split(":")[1]

    def test_different_urls_different_hashes(self) -> None:
        a = _stable_article_id("https://x.com/a", "1")
        b = _stable_article_id("https://x.com/b", "1")
        assert a != b


# ---------- GrokEmailSource ----------


class TestGrokEmailSource:
    @pytest.mark.asyncio
    async def test_no_emails_returns_empty(self) -> None:
        imap = AsyncMock()
        imap.fetch_unread.return_value = []
        grok = AsyncMock()
        extractor = AsyncMock()
        source = GrokEmailSource(imap, grok, extractor)
        assert await source.fetch(max_count=5) == []

    @pytest.mark.asyncio
    async def test_unsubscribe_url_filtered(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """grok.com の同一ドメインでも /http/task-unsubscribe は除外する。"""
        imap = AsyncMock()
        imap.fetch_unread.return_value = [
            _email(
                urls=[
                    "https://grok.com/chat/abc-def",
                    "https://grok.com/http/task-unsubscribe?task=xyz",
                ],
            ),
        ]
        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="https://grok.com/chat/abc-def",
            final_url="https://grok.com/chat/abc-def",
            html="<html>x</html>",
            body_text="ok",
            success=True,
        )
        extractor = MagicMock()
        monkeypatch.setattr(
            "src.tools.source_router._trafilatura_extract",
            lambda html, **kwargs: "x",
            raising=False,
        )
        source = GrokEmailSource(imap, grok, extractor)
        articles = await source.fetch(max_count=5)
        # /chat/ のみ取得され、/http/task-unsubscribe は除外される
        assert len(articles) == 1
        # grok.fetch が unsubscribe URL では呼ばれていないことを確認
        called_urls = [c.kwargs.get("url") or c.args[0] for c in grok.fetch.call_args_list]
        assert all("task-unsubscribe" not in u for u in called_urls)

    @pytest.mark.asyncio
    async def test_one_email_one_url_produces_article(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        imap = AsyncMock()
        imap.fetch_unread.return_value = [_email(urls=["https://grok.com/share/abc"])]

        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="https://grok.com/share/abc",
            final_url="https://grok.com/share/abc",
            html="<html><body>real article body content with enough text</body></html>",
            title="Grok Report A",
            success=True,
        )

        extractor = MagicMock()

        # trafilatura.extract が「ちゃんと本文を返す」モック
        monkeypatch.setattr(
            "src.tools.source_router._trafilatura_extract",
            lambda html, **kwargs: "extracted body text from grok",
            raising=False,
        )

        source = GrokEmailSource(imap, grok, extractor)
        articles = await source.fetch(max_count=5)

        assert len(articles) == 1
        assert articles[0].title == "Grok Report A"
        assert articles[0].id.startswith("grok:")
        assert articles[0].feed_title == "Grok"

    @pytest.mark.asyncio
    async def test_marks_email_seen_after_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        imap = AsyncMock()
        imap.fetch_unread.return_value = [_email(uid="7", urls=["https://grok.com/share/abc"])]
        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="https://grok.com/share/abc",
            final_url="https://grok.com/share/abc",
            html="<html><body>real article body content</body></html>",
            title="R",
            success=True,
        )
        monkeypatch.setattr(
            "src.tools.source_router._trafilatura_extract",
            lambda html, **kwargs: "extracted body text from grok",
            raising=False,
        )
        source = GrokEmailSource(imap, grok, MagicMock(), mark_as_read=True)
        await source.fetch(max_count=5)
        # fetch は非破壊 (mark_as_read=False で取得)、成功後に明示既読化
        assert imap.fetch_unread.call_args.kwargs["mark_as_read"] is False
        imap.mark_seen.assert_awaited_once_with(["7"])

    @pytest.mark.asyncio
    async def test_does_not_mark_seen_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        imap = AsyncMock()
        imap.fetch_unread.return_value = [_email(uid="7", urls=["https://grok.com/share/abc"])]
        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="https://grok.com/share/abc",
            final_url="https://grok.com/share/abc",
            html="",
            title="",
            success=False,
            failure_reason="fetch_failed",
        )
        source = GrokEmailSource(imap, grok, MagicMock(), mark_as_read=True)
        articles = await source.fetch(max_count=5)
        assert articles == []
        imap.mark_seen.assert_not_called()  # 抽出失敗メールは未読に残す (再試行可能)

    @pytest.mark.asyncio
    async def test_mark_as_read_false_skips_marking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        imap = AsyncMock()
        imap.fetch_unread.return_value = [_email(uid="7", urls=["https://grok.com/share/abc"])]
        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="https://grok.com/share/abc",
            final_url="https://grok.com/share/abc",
            html="<html><body>body</body></html>",
            title="R",
            success=True,
        )
        monkeypatch.setattr(
            "src.tools.source_router._trafilatura_extract",
            lambda html, **kwargs: "body",
            raising=False,
        )
        source = GrokEmailSource(imap, grok, MagicMock(), mark_as_read=False)
        await source.fetch(max_count=5)
        imap.mark_seen.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_body_text_when_present(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GrokFetchResult.body_text があれば trafilatura ではなくそれを優先。"""
        imap = AsyncMock()
        imap.fetch_unread.return_value = [_email(urls=["https://grok.com/chat/x"])]

        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="https://grok.com/chat/x",
            final_url="https://grok.com/chat/x",
            html="<html>spa shell</html>",
            body_text="【全体概要】 actual hydrated assistant response from DOM",
            title="Daily CTI",
            success=True,
        )
        extractor = MagicMock()

        called: dict[str, bool] = {"trafilatura": False}

        def fake_trafilatura(html: str, **kwargs: object) -> str:
            called["trafilatura"] = True
            return "should not be used"

        monkeypatch.setattr(
            "src.tools.source_router._trafilatura_extract",
            fake_trafilatura,
            raising=False,
        )

        source = GrokEmailSource(imap, grok, extractor)
        articles = await source.fetch(max_count=5)

        assert len(articles) == 1
        # body_text が summary_html に積み込まれていること
        assert "全体概要" in articles[0].summary_html
        assert "actual hydrated" in articles[0].summary_html
        # body_text を採用したので trafilatura は呼ばれない
        assert called["trafilatura"] is False

    @pytest.mark.asyncio
    async def test_falls_back_to_trafilatura_when_body_text_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """body_text が空なら trafilatura で抽出する。"""
        imap = AsyncMock()
        imap.fetch_unread.return_value = [_email(urls=["https://grok.com/chat/x"])]

        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="https://grok.com/chat/x",
            final_url="https://grok.com/chat/x",
            html="<html><body>fallback HTML body</body></html>",
            body_text="",  # 空
            title="x",
            success=True,
        )
        extractor = MagicMock()
        monkeypatch.setattr(
            "src.tools.source_router._trafilatura_extract",
            lambda html, **kwargs: "extracted by trafilatura",
            raising=False,
        )

        source = GrokEmailSource(imap, grok, extractor)
        articles = await source.fetch(max_count=5)

        assert len(articles) == 1
        assert articles[0].summary_html == "extracted by trafilatura"

    @pytest.mark.asyncio
    async def test_filters_urls_outside_host_allowlist(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # メール本文に boilerplate URL (w3.org) と本物の grok.com URL が混在
        imap = AsyncMock()
        imap.fetch_unread.return_value = [
            _email(
                urls=[
                    "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd",
                    "http://www.w3.org/1999/xhtml",
                    "https://grok.com/chat/abc-123",
                ],
            ),
        ]
        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="https://grok.com/chat/abc-123",
            final_url="https://grok.com/chat/abc-123",
            html="<html><body>real grok content with enough text</body></html>",
            title="Grok",
            success=True,
        )
        extractor = MagicMock()
        monkeypatch.setattr(
            "src.tools.source_router._trafilatura_extract",
            lambda html, **kwargs: "extracted body text",
            raising=False,
        )

        source = GrokEmailSource(imap, grok, extractor)
        articles = await source.fetch(max_count=5)

        # grok.com の URL のみ取得され、w3.org は呼ばれない
        assert len(articles) == 1
        grok.fetch.assert_called_once_with("https://grok.com/chat/abc-123")

    @pytest.mark.asyncio
    async def test_skips_email_with_no_allowlisted_urls(self) -> None:
        imap = AsyncMock()
        imap.fetch_unread.return_value = [
            _email(urls=["http://www.w3.org/1999/xhtml"]),
        ]
        grok = AsyncMock()
        extractor = MagicMock()

        source = GrokEmailSource(imap, grok, extractor)
        articles = await source.fetch(max_count=5)

        assert articles == []
        grok.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_custom_host_allowlist_overrides_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        imap = AsyncMock()
        imap.fetch_unread.return_value = [
            _email(urls=["https://example.com/report/x", "https://grok.com/chat/y"]),
        ]
        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="https://example.com/report/x",
            final_url="https://example.com/report/x",
            html="<html><body>example body content</body></html>",
            title="Example",
            success=True,
        )
        extractor = MagicMock()
        monkeypatch.setattr(
            "src.tools.source_router._trafilatura_extract",
            lambda html, **kwargs: "extracted body",
            raising=False,
        )

        source = GrokEmailSource(
            imap,
            grok,
            extractor,
            url_host_allowlist=["example.com"],
        )
        articles = await source.fetch(max_count=5)

        assert len(articles) == 1
        grok.fetch.assert_called_once_with("https://example.com/report/x")

    @pytest.mark.asyncio
    async def test_failed_grok_fetch_skipped(self) -> None:
        imap = AsyncMock()
        imap.fetch_unread.return_value = [_email()]

        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="x",
            final_url="x",
            success=False,
            failure_reason="session_expired",
        )

        extractor = AsyncMock()
        source = GrokEmailSource(imap, grok, extractor)
        articles = await source.fetch(max_count=5)
        assert articles == []

    @pytest.mark.asyncio
    async def test_max_count_caps_articles(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 1 メールに 5 URL
        imap = AsyncMock()
        imap.fetch_unread.return_value = [
            _email(urls=[f"https://grok.com/share/{i}" for i in range(5)]),
        ]
        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="x",
            final_url="x",
            html="<p>body</p>",
            success=True,
        )
        extractor = MagicMock()
        monkeypatch.setattr(
            "src.tools.source_router._trafilatura_extract",
            lambda html, **kwargs: "body",
            raising=False,
        )

        source = GrokEmailSource(imap, grok, extractor)
        articles = await source.fetch(max_count=2)
        assert len(articles) == 2

    @pytest.mark.asyncio
    async def test_one_url_failure_does_not_break_others(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        imap = AsyncMock()
        imap.fetch_unread.return_value = [
            _email(urls=["https://bad.com/", "https://grok.com/ok"]),
        ]
        grok = AsyncMock()

        async def _fetch(url: str) -> GrokFetchResult:
            if url == "https://bad.com/":
                raise RuntimeError("network")
            return GrokFetchResult(
                url=url,
                final_url=url,
                html="<p>ok</p>",
                success=True,
            )

        grok.fetch.side_effect = _fetch
        extractor = MagicMock()
        monkeypatch.setattr(
            "src.tools.source_router._trafilatura_extract",
            lambda html, **kwargs: "body",
            raising=False,
        )

        source = GrokEmailSource(imap, grok, extractor)
        articles = await source.fetch(max_count=5)
        assert len(articles) == 1


# ---------- build_source factory ----------


class TestBuildSource:
    def test_builds_grok_email_source(self) -> None:
        cfg = SourceConfig(
            type="grok_email",
            max_articles=10,
            grok_sender_filters=["x.ai"],
            grok_subject_filters=["Grok"],
        )
        source = build_source(
            cfg,
            imap_client=MagicMock(),
            grok_fetcher=MagicMock(),
            extractor=MagicMock(),
        )
        assert isinstance(source, GrokEmailSource)

    def test_grok_email_requires_all_deps(self) -> None:
        cfg = SourceConfig(type="grok_email", max_articles=10)
        with pytest.raises(ValueError, match="grok_email"):
            build_source(cfg, imap_client=MagicMock())

    def test_rss_source_implemented(self) -> None:
        """Phase X-1: rss source は DirectRssSource として実装済。"""
        from src.tools.direct_rss_source import DirectRssSource

        cfg = SourceConfig(type="rss", max_articles=10)
        source = build_source(cfg)
        assert isinstance(source, DirectRssSource)

    def test_unknown_type_rejected(self) -> None:
        # config_loader が Literal で型ガードしているが、念のため動的検証
        cfg = MagicMock()
        cfg.type = "unknown_type"
        cfg.stream_id = None
        cfg.feeds = []
        cfg.grok_sender_filters = []
        cfg.grok_subject_filters = []
        cfg.grok_lookback_minutes = None
        with pytest.raises(ValueError, match="未知の source type"):
            build_source(cfg)


class TestWebScraperClusterAutoCollect:
    """統合再設計: cluster は yaml ソースを registry から auto-collect し、
    pipelines.yaml には custom .py のみ列挙する。enabled=entry.yaml が SSoT。"""

    def _patch_registries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        scr_enabled: list[str],
        wat_enabled: list[str],
        scr_all: list[str],
        wat_all: list[str],
    ) -> None:
        import src.watchers.scrapers_registry as sr
        import src.watchers.yaml_registry as wr

        def _cfg_obj(key: str, names: list[str]) -> object:
            return type("Cfg", (), {key: [type("E", (), {"name": n})() for n in names]})()

        monkeypatch.setattr(sr, "get_registry", lambda: dict.fromkeys(scr_enabled))
        monkeypatch.setattr(wr, "get_registry", lambda: dict.fromkeys(wat_enabled))
        monkeypatch.setattr(sr, "load_scrapers_config", lambda: _cfg_obj("scrapers", scr_all))
        monkeypatch.setattr(wr, "load_watchers_config", lambda: _cfg_obj("watchers", wat_all))

    def test_auto_collects_enabled_yaml_and_custom_py(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.config_loader import ScraperEntry

        self._patch_registries(
            monkeypatch,
            scr_enabled=["merics-org"],
            wat_enabled=["enisa", "ipa"],
            scr_all=["merics-org"],
            wat_all=["enisa", "ipa", "hudson"],  # hudson disabled
        )
        # pipelines.yaml には custom .py のみ + 紛れ込んだ yaml 名 (無視されるべき)
        cfg = SourceConfig(
            type="web_scraper_cluster",
            scrapers=[
                ScraperEntry(name="nicter", enabled=True),  # custom enabled
                ScraperEntry(name="38north", enabled=False),  # custom disabled
                ScraperEntry(name="hudson", enabled=True),  # yaml 名 (無視 = registry が決定)
            ],
            max_articles_per_scraper=8,
        )
        src = cast(WebScraperClusterSource, build_source(cfg))
        active = sorted(n for n, e in src.all_scrapers if e)
        # auto: merics-org, enisa, ipa (hudson は registry disabled で除外) + custom: nicter
        assert active == ["enisa", "ipa", "merics-org", "nicter"]
        # hudson は explicit に enabled=True で居たが yaml 名なので registry 判定 (disabled) が勝つ
        assert "hudson" not in active
        # 38north は custom disabled
        assert "38north" not in active
