"""src/tools/mobile_tunnel_files.py の unit test (tunnel との data/ ファイル契約)。

不変条件:
- token は **0600** で書かれる (最前線 tunnel コンテナには 1 ファイルだけ渡す = least-privilege)。
- token の生値は返すヘルパを持たない (:func:`is_token_set` は boolean のみ)。
- mode は enabled flag と token の有無から named/quick/off を導出する。
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from src.tools import mobile_tunnel_files as mtf


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    token = tmp_path / ".mobile_tunnel_token"
    hostname = tmp_path / ".mobile_tunnel_hostname"
    flag = tmp_path / ".mobile_tunnel_enabled"
    monkeypatch.setattr(mtf, "TOKEN_FILE", token)
    monkeypatch.setattr(mtf, "HOSTNAME_FILE", hostname)
    monkeypatch.setattr(mtf, "ENABLED_FLAG_FILE", flag)
    return {"token": token, "hostname": hostname, "flag": flag}


class TestToken:
    def test_write_then_is_set(self, files: dict[str, Path]) -> None:
        assert mtf.is_token_set() is False
        mtf.write_token("eyJ" + "a" * 60)
        assert mtf.is_token_set() is True
        assert files["token"].read_text() == "eyJ" + "a" * 60

    def test_token_file_is_0600(self, files: dict[str, Path]) -> None:
        mtf.write_token("x" * 50)
        assert stat.S_IMODE(files["token"].stat().st_mode) == 0o600

    def test_write_strips_whitespace(self, files: dict[str, Path]) -> None:
        mtf.write_token("  tok-value-123  \n")
        assert files["token"].read_text() == "tok-value-123"

    def test_empty_write_clears(self, files: dict[str, Path]) -> None:
        mtf.write_token("abc")
        mtf.write_token("   ")
        assert mtf.is_token_set() is False
        assert not files["token"].exists()

    def test_clear_is_idempotent(self, files: dict[str, Path]) -> None:
        mtf.clear_token()  # 不在でも例外を出さない
        mtf.write_token("abc")
        mtf.clear_token()
        assert not files["token"].exists()

    def test_atomic_overwrite(self, files: dict[str, Path]) -> None:
        mtf.write_token("first-token-value-xxxxxxxxxxxx")
        mtf.write_token("second-token-value-yyyyyyyyyyy")
        assert files["token"].read_text() == "second-token-value-yyyyyyyyyyy"


class TestHostname:
    def test_write_and_read_normalizes_scheme(self, files: dict[str, Path]) -> None:
        mtf.write_hostname("https://kuebiko.example/")
        assert files["hostname"].read_text() == "kuebiko.example"
        assert mtf.read_hostname() == "kuebiko.example"

    def test_read_missing_returns_none(self, files: dict[str, Path]) -> None:
        assert mtf.read_hostname() is None

    def test_empty_write_clears(self, files: dict[str, Path]) -> None:
        mtf.write_hostname("kuebiko.example")
        mtf.write_hostname("")
        assert mtf.read_hostname() is None

    def test_hostname_file_is_world_readable(self, files: dict[str, Path]) -> None:
        # 非秘密 (public hostname) なので 0644 (tunnel コンテナ root からも読める)
        mtf.write_hostname("kuebiko.example")
        assert stat.S_IMODE(files["hostname"].stat().st_mode) == 0o644


class TestMode:
    def test_off_when_disabled(self, files: dict[str, Path]) -> None:
        assert mtf.mode() == "off"

    def test_quick_when_enabled_without_token(self, files: dict[str, Path]) -> None:
        files["flag"].touch()
        assert mtf.mode() == "quick"

    def test_named_when_enabled_with_token(self, files: dict[str, Path]) -> None:
        files["flag"].touch()
        mtf.write_token("z" * 50)
        assert mtf.mode() == "named"
