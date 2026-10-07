"""運用画面の未ログインの入口が公開版へ案内するための public_site_origin (2026-10-08)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ui.api import pages


def test_public_site_origin_is_read_from_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: .env はファイルとしてコンテナに渡る (環境変数ではない)
    (tmp_path / ".env").write_text("PUBLIC_SITE_ORIGIN=https://kuebiko.example\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PUBLIC_SITE_ORIGIN", raising=False)

    # Act / Assert
    assert pages._public_site_origin() == "https://kuebiko.example"


def test_public_site_origin_rejects_non_https(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("PUBLIC_SITE_ORIGIN=javascript:alert(1)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PUBLIC_SITE_ORIGIN", raising=False)

    assert pages._public_site_origin() == ""


def test_public_site_origin_empty_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PUBLIC_SITE_ORIGIN", raising=False)

    assert pages._public_site_origin() == ""
