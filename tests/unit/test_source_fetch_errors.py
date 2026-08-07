"""ソース追加ウィザードの fetch エラー UX (early-fail + 分かりやすいメッセージ) のテスト。"""

from __future__ import annotations

import httpx
import pytest

from src.ui.api._source_discovery import discover_sources
from src.ui.api._source_http import SourceFetchError, fetch_text


class TestFriendlyFetchError:
    def test_timeout_raises_friendly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(self: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        monkeypatch.setattr(httpx.Client, "get", boom)
        with pytest.raises(SourceFetchError) as ei:
            fetch_text("https://www.sophos.com/en-us/blog?page=1")
        msg = str(ei.value)
        assert "タイムアウト" in msg
        assert "sophos.com" in msg  # ホスト名を提示

    def test_connect_error_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(self: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        # SSRF guard は本テストの対象外 (解決不能ホストで網羅) なので無効化し、
        # ネットワークエラー文言の変換のみを検証する。
        monkeypatch.setattr("src.ui.api._source_http.assert_safe_public_url", lambda url: None)
        monkeypatch.setattr(httpx.Client, "get", boom)
        with pytest.raises(SourceFetchError) as ei:
            fetch_text("https://nope.example/")
        assert "接続できませんでした" in str(ei.value)

    def test_read_error_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(self: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
            raise httpx.ReadError("connection reset")

        monkeypatch.setattr("src.ui.api._source_http.assert_safe_public_url", lambda url: None)
        monkeypatch.setattr(httpx.Client, "get", boom)
        with pytest.raises(SourceFetchError) as ei:
            fetch_text("https://blocked.example/")
        assert "切断" in str(ei.value)


class TestDiscoverFailFast:
    def test_failfast_on_network_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ネットワーク到達不可なら同一ホストを何度も probe せず 1 回で fail。"""
        calls: list[str] = []

        def boom(url: str, timeout: float = 20.0) -> tuple[int, str, dict[str, str]]:
            calls.append(url)
            raise SourceFetchError("sophos.com が時間内に応答しませんでした (タイムアウト)。")

        monkeypatch.setattr("src.ui.api._source_discovery.fetch_text", boom)
        candidates, notes = discover_sources("https://www.sophos.com/en-us/blog?page=1")

        assert candidates == []
        assert len(calls) == 1  # path probe (Step D/E) を踏まず即 return
        assert any("応答しませんでした" in n for n in notes)
