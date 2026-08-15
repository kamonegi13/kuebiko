"""外部公開 URL 解決 (src/tools/public_url.py) の unit test。

優先順: named tunnel env (CLOUDFLARE_TUNNEL_HOSTNAME) > quick tunnel URL file > None。
env を設定するだけで quick → named に自動移行するのが要件 (2026-07-12)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools import mobile_tunnel_files as mtf
from src.tools.public_url import resolve_public_base_url

_ENV = "CLOUDFLARE_TUNNEL_HOSTNAME"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """env と hostname ファイルを既定で無効化 (host の /app 状態に依存しない)。"""
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setattr(mtf, "HOSTNAME_FILE", tmp_path / "_absent_hostname")


def _url_file(tmp_path: Path, content: str | None) -> Path:
    f = tmp_path / ".mobile_tunnel_url"
    if content is not None:
        f.write_text(content, encoding="utf-8")
    return f


class TestNamedTunnelEnv:
    def test_hostname_becomes_https_base(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_ENV, "cti.example.com")
        base = resolve_public_base_url(url_file=_url_file(tmp_path, None))
        assert base == "https://cti.example.com"

    def test_scheme_prefix_is_normalized(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_ENV, "https://cti.example.com/")
        base = resolve_public_base_url(url_file=_url_file(tmp_path, None))
        assert base == "https://cti.example.com"

    def test_env_wins_over_quick_tunnel_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_ENV, "cti.example.com")
        f = _url_file(tmp_path, "https://random.trycloudflare.com")
        assert resolve_public_base_url(url_file=f) == "https://cti.example.com"

    def test_blank_env_falls_through_to_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_ENV, "   ")
        f = _url_file(tmp_path, "https://random.trycloudflare.com")
        assert resolve_public_base_url(url_file=f) == "https://random.trycloudflare.com"


class TestNamedTunnelHostnameFile:
    """data/.mobile_tunnel_hostname (UI 管理) が env / quick URL より優先される (C2)。"""

    def _hostname_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str) -> None:
        hf = tmp_path / ".mobile_tunnel_hostname"
        hf.write_text(value, encoding="utf-8")
        monkeypatch.setattr(mtf, "HOSTNAME_FILE", hf)

    def test_hostname_file_becomes_https_base(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._hostname_file(monkeypatch, tmp_path, "kuebiko.example")
        assert (
            resolve_public_base_url(url_file=_url_file(tmp_path, None)) == "https://kuebiko.example"
        )

    def test_file_wins_over_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._hostname_file(monkeypatch, tmp_path, "kuebiko.example")
        monkeypatch.setenv(_ENV, "old-env-host.example.com")
        assert (
            resolve_public_base_url(url_file=_url_file(tmp_path, None)) == "https://kuebiko.example"
        )

    def test_file_wins_over_quick_url(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._hostname_file(monkeypatch, tmp_path, "https://kuebiko.example/")
        f = _url_file(tmp_path, "https://random.trycloudflare.com")
        assert resolve_public_base_url(url_file=f) == "https://kuebiko.example"


class TestQuickTunnelFile:
    def test_reads_current_quick_tunnel_url(self, tmp_path: Path) -> None:
        f = _url_file(tmp_path, "https://ten-goal-but-extend.trycloudflare.com\n")
        assert (
            resolve_public_base_url(url_file=f) == "https://ten-goal-but-extend.trycloudflare.com"
        )

    def test_trailing_slash_is_stripped(self, tmp_path: Path) -> None:
        f = _url_file(tmp_path, "https://x.trycloudflare.com/")
        assert resolve_public_base_url(url_file=f) == "https://x.trycloudflare.com"

    def test_garbage_content_returns_none(self, tmp_path: Path) -> None:
        f = _url_file(tmp_path, "starting cloudflared...")
        assert resolve_public_base_url(url_file=f) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert resolve_public_base_url(url_file=_url_file(tmp_path, None)) is None

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        assert resolve_public_base_url(url_file=_url_file(tmp_path, "")) is None
