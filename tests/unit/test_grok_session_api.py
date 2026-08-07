"""Grok セッション管理 API のテスト (2026-07-05)。

UI からの再取得フロー (状態可視化 → 再取得ガイド → 完了自動検知 → 検証) の backend と、
セッション失効の errors 搬送 (監査残項目①: 「🟢 取得 0」化けの根治) を回帰固定する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.grok.fetcher import GrokFetchResult
from src.tools.source_router import GrokEmailSource
from src.ui.api import grok_session as gs


class TestSessionStatus:
    @pytest.mark.asyncio
    async def test_no_state_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gs, "_STATE_PATH", tmp_path / "missing.json")
        monkeypatch.setattr(gs, "_VERIFY_STATUS_PATH", tmp_path / "verify.json")
        monkeypatch.setattr(gs, "_last_grok_run", lambda: None)
        out = await gs.get_session_status()
        assert out["state"] == {"exists": False}
        assert out["acquire_command"].startswith("uv run")

    @pytest.mark.asyncio
    async def test_status_never_leaks_cookie_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # セキュリティ §12: cookie の値も名前も応答に含めない (件数とドメイン集計のみ)
        state = tmp_path / "state.json"
        secret_value = "super-secret-session-token-abc123"
        state.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "sso", "value": secret_value, "domain": ".grok.com"},
                        {"name": "auth_token", "value": "tok2", "domain": ".x.ai"},
                        {"name": "misc", "value": "tok3", "domain": ".grok.com"},
                    ]
                }
            )
        )
        monkeypatch.setattr(gs, "_STATE_PATH", state)
        monkeypatch.setattr(gs, "_VERIFY_STATUS_PATH", tmp_path / "verify.json")
        monkeypatch.setattr(gs, "_last_grok_run", lambda: None)
        out = await gs.get_session_status()
        serialized = json.dumps(out)
        assert secret_value not in serialized
        assert "sso" not in serialized  # cookie 名も出さない
        assert out["state"]["cookie_count"] == 3
        assert out["state"]["domains"] == {"grok.com": 2, "x.ai": 1}


class _FakeFetcher:
    def __init__(self, result: GrokFetchResult) -> None:
        self._result = result

    async def __aenter__(self) -> _FakeFetcher:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def fetch(self, url: str) -> GrokFetchResult:
        return self._result


class TestVerify:
    @pytest.mark.asyncio
    async def test_ok_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state = tmp_path / "state.json"
        state.write_text('{"cookies": []}')
        monkeypatch.setattr(gs, "_STATE_PATH", state)
        monkeypatch.setattr(gs, "_VERIFY_STATUS_PATH", tmp_path / "verify.json")
        result = GrokFetchResult(
            url="https://grok.com", final_url="https://grok.com/", success=True
        )
        monkeypatch.setattr("src.grok.fetcher.GrokFetcher", lambda: _FakeFetcher(result))
        out = await gs.verify_session()
        assert out["status"] == "ok"
        # sidecar に永続化され GET が返す
        saved = json.loads((tmp_path / "verify.json").read_text())
        assert saved["status"] == "ok"

    @pytest.mark.asyncio
    async def test_expired_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state = tmp_path / "state.json"
        state.write_text('{"cookies": []}')
        monkeypatch.setattr(gs, "_STATE_PATH", state)
        monkeypatch.setattr(gs, "_VERIFY_STATUS_PATH", tmp_path / "verify.json")
        result = GrokFetchResult(
            url="https://grok.com",
            final_url="https://accounts.x.ai/sign-in",
            success=False,
            failure_reason="session_expired",
        )
        monkeypatch.setattr("src.grok.fetcher.GrokFetcher", lambda: _FakeFetcher(result))
        out = await gs.verify_session()
        assert out["status"] == "session_expired"

    @pytest.mark.asyncio
    async def test_no_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gs, "_STATE_PATH", tmp_path / "missing.json")
        monkeypatch.setattr(gs, "_VERIFY_STATUS_PATH", tmp_path / "verify.json")
        out = await gs.verify_session()
        assert out["status"] == "no_state"


class TestSessionExpiredCounting:
    """監査残項目①: セッション失効が「🟢 取得 0」に化ける問題の検出側。"""

    @pytest.mark.asyncio
    async def test_source_counts_session_expired(self) -> None:
        from tests.unit.test_source_router import _email

        imap = AsyncMock()
        imap.fetch_unread.return_value = [_email(urls=["https://grok.com/chat/abc"])]
        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="https://grok.com/chat/abc",
            final_url="https://accounts.x.ai/sign-in",
            success=False,
            failure_reason="session_expired",
        )
        source = GrokEmailSource(imap, grok, AsyncMock())
        articles = await source.fetch(max_count=5)
        assert articles == []
        assert source.last_session_expired_count == 1

    @pytest.mark.asyncio
    async def test_counter_resets_per_fetch(self) -> None:
        from tests.unit.test_source_router import _email

        imap = AsyncMock()
        imap.fetch_unread.return_value = [_email(urls=["https://grok.com/chat/abc"])]
        grok = AsyncMock()
        grok.fetch.return_value = GrokFetchResult(
            url="https://grok.com/chat/abc",
            final_url="https://accounts.x.ai/sign-in",
            success=False,
            failure_reason="session_expired",
        )
        source = GrokEmailSource(imap, grok, AsyncMock())
        await source.fetch(max_count=5)
        imap.fetch_unread.return_value = []
        await source.fetch(max_count=5)
        assert source.last_session_expired_count == 0
