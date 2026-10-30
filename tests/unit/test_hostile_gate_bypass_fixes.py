"""敵対的 host の取得ポリシーを迂回していた経路 (visual picker / UA 健全性 canary) の閉鎖。

- picker: policy=on では hostile host に Playwright を使わない / PAUSE では取得自体しない /
  既定 (shadow・0) は挙動不変
- ua-health canary: policy=on / PAUSE では hostile を probe しない / shadow は probe してログに残す
- 旧 UA (BOT_UA_NEUTRAL=0) は中立化前の値そのものに戻る
- ローカル層が無いとき本番では warning (テストでは info)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import structlog.testing

from src import local_overlay
from src.sources import ransomware_live
from src.tools import hostile_fetch
from src.ui.api import _source_proxy
from src.ui.services import ua_health

_HOSTILE = "https://news.example.ru/list"
_NORMAL = "https://blog.kuebiko.example/list"
_SHELL = "<html><body><div id='root'></div></body></html>"
_PAGE = "<html><body><a href='/a'>a</a></body></html>"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (hostile_fetch.ENV_POLICY, hostile_fetch.ENV_PAUSE, "BOT_UA_NEUTRAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_source_proxy, "assert_safe_public_url", lambda url: None)


@pytest.fixture
def renderer(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=_PAGE)
    monkeypatch.setattr("src.tools.page_renderer.render_html", mock)
    return mock


def _static_fetch(monkeypatch: pytest.MonkeyPatch, html: str) -> list[str]:
    calls: list[str] = []

    def fake(url: str, timeout: float = 20.0) -> tuple[int, str, dict[str, str], str]:
        calls.append(url)
        return 200, html, {}, "bot"

    monkeypatch.setattr("src.ui.api._source_http.fetch_text", fake)
    return calls


class TestPickerHostileGate:
    @pytest.mark.asyncio
    async def test_policy_on_blocks_js_render_on_hostile_host(
        self, monkeypatch: pytest.MonkeyPatch, renderer: AsyncMock
    ) -> None:
        monkeypatch.setenv(hostile_fetch.ENV_POLICY, "1")
        _static_fetch(monkeypatch, _SHELL)
        with pytest.raises(_source_proxy.PickerBlockedError) as exc:
            await _source_proxy.build_proxy_html(_HOSTILE, render_mode="js")
        assert "ブラウザ描画" in str(exc.value)
        renderer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_policy_on_blocks_auto_fallback_to_js(
        self, monkeypatch: pytest.MonkeyPatch, renderer: AsyncMock
    ) -> None:
        monkeypatch.setenv(hostile_fetch.ENV_POLICY, "1")
        _static_fetch(monkeypatch, _SHELL)
        with pytest.raises(_source_proxy.PickerBlockedError):
            await _source_proxy.build_proxy_html(_HOSTILE, render_mode="auto")
        renderer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_policy_on_still_allows_static_html(
        self, monkeypatch: pytest.MonkeyPatch, renderer: AsyncMock
    ) -> None:
        monkeypatch.setenv(hostile_fetch.ENV_POLICY, "1")
        calls = _static_fetch(monkeypatch, _PAGE)
        out = await _source_proxy.build_proxy_html(_HOSTILE, render_mode="static")
        assert calls == [_HOSTILE]
        assert "<a" in out
        renderer.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["static", "js", "auto"])
    async def test_pause_blocks_any_fetch_on_hostile_host(
        self, monkeypatch: pytest.MonkeyPatch, renderer: AsyncMock, mode: str
    ) -> None:
        monkeypatch.setenv(hostile_fetch.ENV_PAUSE, "1")
        calls = _static_fetch(monkeypatch, _PAGE)
        with pytest.raises(_source_proxy.PickerBlockedError) as exc:
            await _source_proxy.build_proxy_html(_HOSTILE, render_mode=mode)
        assert "停止スイッチ" in str(exc.value)
        assert calls == []
        renderer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_shadow_keeps_js_render_and_logs(
        self, monkeypatch: pytest.MonkeyPatch, renderer: AsyncMock
    ) -> None:
        _static_fetch(monkeypatch, _SHELL)
        with structlog.testing.capture_logs() as logs:
            out = await _source_proxy.build_proxy_html(_HOSTILE, render_mode="js")
        renderer.assert_awaited_once()
        assert "<a" in out
        assert any(
            e["event"] == "hostile_host_policy" and e["action"] == "would_skip_playwright"
            for e in logs
        )

    @pytest.mark.asyncio
    async def test_non_hostile_host_unaffected_by_policy_and_pause(
        self, monkeypatch: pytest.MonkeyPatch, renderer: AsyncMock
    ) -> None:
        monkeypatch.setenv(hostile_fetch.ENV_POLICY, "1")
        monkeypatch.setenv(hostile_fetch.ENV_PAUSE, "1")
        _static_fetch(monkeypatch, _SHELL)
        await _source_proxy.build_proxy_html(_NORMAL, render_mode="js")
        renderer.assert_awaited_once()

    def test_endpoint_returns_403_with_ui_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        from fastapi import HTTPException

        from src.ui.api import sources

        monkeypatch.setattr(sources, "_READ_ONLY", False)
        monkeypatch.setenv(hostile_fetch.ENV_PAUSE, "1")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(sources.proxy_page(_HOSTILE))
        assert exc.value.status_code == 403
        assert "停止スイッチ" in str(exc.value.detail)


class TestUaHealthCanaries:
    urls = [_HOSTILE, _NORMAL]

    def test_policy_on_excludes_hostile_canary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(hostile_fetch.ENV_POLICY, "1")
        with structlog.testing.capture_logs() as logs:
            kept = ua_health.exclude_hostile_canaries(self.urls)
        assert kept == [_NORMAL]
        assert [e["action"] for e in logs if e["event"] == "ua_health_hostile_canary"] == [
            "excluded"
        ]

    def test_pause_excludes_hostile_canary_in_any_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(hostile_fetch.ENV_POLICY, "0")
        monkeypatch.setenv(hostile_fetch.ENV_PAUSE, "1")
        assert ua_health.exclude_hostile_canaries(self.urls) == [_NORMAL]

    def test_shadow_keeps_hostile_canary_but_logs(self) -> None:
        with structlog.testing.capture_logs() as logs:
            kept = ua_health.exclude_hostile_canaries(self.urls)
        assert kept == self.urls
        assert [e["action"] for e in logs if e["event"] == "ua_health_hostile_canary"] == [
            "would_exclude"
        ]

    @pytest.mark.asyncio
    async def test_run_check_without_remaining_canaries_reports_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(hostile_fetch.ENV_POLICY, "1")

        class _Repo:
            def sample_healthy_source_urls(self, limit: int = 8) -> list[str]:
                return [_HOSTILE]

        out = await ua_health.run_ua_health_check(_Repo())  # type: ignore[arg-type]
        assert out == {"status": "no_canaries", "canaries": 0}


class TestLegacyRansomwareUa:
    def test_neutral_default_has_no_project_name(self) -> None:
        assert "kuebiko" not in ransomware_live._user_agent().lower()

    def test_flag_off_restores_exact_previous_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_UA_NEUTRAL", "0")
        assert ransomware_live._user_agent() == "kuebiko-bot"


class TestMissingOverlayLogLevel:
    def test_levels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, str, dict[str, object]]] = []

        class _Logger:
            def info(self, event: str, **kw: object) -> None:
                calls.append(("info", event, kw))

            def warning(self, event: str, **kw: object) -> None:
                calls.append(("warning", event, kw))

        monkeypatch.setattr(local_overlay, "is_test_environment", lambda: False)
        local_overlay.log_layers_loaded(_Logger(), "e", local_used=False, n=1)
        local_overlay.log_layers_loaded(_Logger(), "e", local_used=True, n=2)
        monkeypatch.setattr(local_overlay, "is_test_environment", lambda: True)
        local_overlay.log_layers_loaded(_Logger(), "e", local_used=False, n=3)

        assert [c[0] for c in calls] == ["warning", "info", "info"]
        assert calls[0][2]["local_missing"] is True
        assert calls[0][2]["source"] == "generic"
