"""/mobile-tunnel/* API の unit test (named tunnel の UI 管理)。

セキュリティ不変条件 (最重要): token の生値は **どのレスポンスにも現れない**
(readonly instance が GET を外部公開するため boolean + public hostname のみ)。
token/hostname の保存は POST (full instance のみ; readonly は write middleware が 403)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.tools import mobile_tunnel_files as mtf
from src.ui.api.pages import pages_api

# 実 cloudflared token 風の長い base64 文字列 (レスポンスに混入していないか検査するのに使う)
_REAL_TOKEN = "eyJhIjoi" + "A" * 180


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(mtf, "TOKEN_FILE", tmp_path / ".mobile_tunnel_token")
    monkeypatch.setattr(mtf, "HOSTNAME_FILE", tmp_path / ".mobile_tunnel_hostname")
    monkeypatch.setattr(mtf, "ENABLED_FLAG_FILE", tmp_path / ".mobile_tunnel_enabled")
    app = FastAPI()
    app.include_router(pages_api)
    return TestClient(app)


def test_status_shape_off(client: TestClient) -> None:
    body = client.get("/api/v1/mobile-tunnel/status").json()
    assert body["mode"] == "off"
    assert body["token_set"] is False
    assert body["hostname"] is None


def test_set_named_config_persists_but_never_returns_token(client: TestClient) -> None:
    r = client.post(
        "/api/v1/mobile-tunnel/named-config",
        json={"token": _REAL_TOKEN, "hostname": "kuebiko.example"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token_set"] is True
    assert body["hostname"] == "kuebiko.example"
    # 最重要: 生 token がレスポンス本文のどこにも現れない
    assert _REAL_TOKEN not in r.text


def test_status_never_leaks_token_even_when_set(client: TestClient) -> None:
    client.post("/api/v1/mobile-tunnel/named-config", json={"token": _REAL_TOKEN})
    r = client.get("/api/v1/mobile-tunnel/status")
    assert _REAL_TOKEN not in r.text
    assert r.json()["token_set"] is True


def test_mode_named_after_enable_and_token(client: TestClient) -> None:
    client.post("/api/v1/mobile-tunnel/enable")
    client.post("/api/v1/mobile-tunnel/named-config", json={"token": _REAL_TOKEN})
    assert client.get("/api/v1/mobile-tunnel/status").json()["mode"] == "named"


def test_empty_token_keeps_existing(client: TestClient) -> None:
    client.post("/api/v1/mobile-tunnel/named-config", json={"token": _REAL_TOKEN})
    # 空 token は既存維持 (hostname だけ更新) — secret の空送信 skip 規約
    client.post(
        "/api/v1/mobile-tunnel/named-config",
        json={"token": "", "hostname": "kuebiko.example"},
    )
    body = client.get("/api/v1/mobile-tunnel/status").json()
    assert body["token_set"] is True
    assert body["hostname"] == "kuebiko.example"


def test_invalid_token_rejected(client: TestClient) -> None:
    r = client.post("/api/v1/mobile-tunnel/named-config", json={"token": "short bad token!!!"})
    assert r.status_code == 400


def test_invalid_hostname_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/v1/mobile-tunnel/named-config",
        json={"hostname": "bad host name/with spaces"},
    )
    assert r.status_code == 400


def test_clear_reverts_to_quick(client: TestClient) -> None:
    client.post("/api/v1/mobile-tunnel/enable")
    client.post(
        "/api/v1/mobile-tunnel/named-config",
        json={"token": _REAL_TOKEN, "hostname": "kuebiko.example"},
    )
    client.post("/api/v1/mobile-tunnel/named-config/clear")
    body = client.get("/api/v1/mobile-tunnel/status").json()
    assert body["token_set"] is False
    assert body["hostname"] is None
    assert body["mode"] == "quick"  # enabled のまま token なし
