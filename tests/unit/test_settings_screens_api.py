"""設定・死活の画面統合 (P1-P4) API テスト。

チャンネル webhook / Grok メール (IMAP) / Ollama URL / システム設定の各 API が
.env 保存層 (allowlist + マスク) を正しく経由することを TestClient で確認する。
外部疎通 (Discord / IMAP / Ollama) は monkeypatch でスタブし、ネットワークに出ない。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _init_git(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)


def _bootstrap_project(tmp_path: Path) -> Path:
    (tmp_path / "prompts" / "briefing").mkdir(parents=True)
    (tmp_path / "prompts" / "briefing/summarizer.j2").write_text("test prompt", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "pipelines.yaml").write_text(
        "pipelines:\n  - name: direct-rss-fetch\n"
        "    source:\n      type: rss\n      max_articles: 10\n"
        "    processor: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    (tmp_path / ".env").write_text(
        "DISCORD_WEBHOOK_ALERT=https://discord.com/api/webhooks/1/a\n"
        "DISCORD_WEBHOOK_BRIEF=https://discord.com/api/webhooks/2/b\n"
        "IMAP_USER=old-user@example.com\n"
        "IMAP_PASSWORD=old-password\n",
        encoding="utf-8",
    )
    _init_git(tmp_path)
    return tmp_path


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    project = _bootstrap_project(tmp_path)
    monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)
    monkeypatch.setenv("DISCORD_WEBHOOK_ALERT", "https://discord.com/api/webhooks/1/a")
    monkeypatch.setenv("DISCORD_WEBHOOK_BRIEF", "https://discord.com/api/webhooks/2/b")
    # config/system・ollama-url 保存が os.environ に同期するため、restore 対象として
    # 先に monkeypatch へ登録する (漏れると後続テストを実 URL/レベルで汚染する)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("TIMEZONE", "Asia/Tokyo")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")

    from src.ui import app as app_module

    fresh_app = app_module.create_app()
    with TestClient(fresh_app) as c:
        yield c


def _env_text() -> str:
    return (Path.cwd() / ".env").read_text(encoding="utf-8")


# ---------- P1: チャンネル webhook ----------


def test_channels_get_returns_masked_webhooks_without_plaintext(client: TestClient) -> None:
    resp = client.get("/api/v1/channels")
    assert resp.status_code == 200
    data = resp.json()
    assert "webhook_masked" in data
    masked = data["webhook_masked"]
    assert masked["alert"].endswith("***")
    # 平文 URL がレスポンス全体に混入しないこと
    assert "https://discord.com/api/webhooks/1/a" not in resp.text


def test_channel_webhook_save_writes_env(client: TestClient) -> None:
    url = "https://discord.com/api/webhooks/999/new-token"
    resp = client.post("/api/v1/channels/watch/webhook", json={"url": url})
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] is True
    assert body["webhook_set"] is True
    assert body["webhook_masked"].endswith("***")
    assert url not in body["webhook_masked"]
    assert f"DISCORD_WEBHOOK_WATCH={url}" in _env_text()


def test_channel_webhook_save_rejects_non_discord_url(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/channels/watch/webhook",
        json={"url": "https://evil.example.com/api/webhooks/1/x"},
    )
    assert resp.status_code == 400


def test_channel_webhook_save_unknown_channel_404(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/channels/nonexistent/webhook",
        json={"url": "https://discord.com/api/webhooks/1/x"},
    )
    assert resp.status_code == 404


def test_channel_webhook_empty_clears_value(client: TestClient) -> None:
    resp = client.post("/api/v1/channels/brief/webhook", json={"url": ""})
    assert resp.status_code == 200
    assert resp.json()["webhook_set"] is False
    assert "DISCORD_WEBHOOK_BRIEF=\n" in _env_text() or "DISCORD_WEBHOOK_BRIEF=" in _env_text()


def test_channels_health_uses_stubbed_check(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.ui.services import health as health_module

    async def fake_check(name: str, url: str) -> health_module.HealthCheck:
        return health_module.HealthCheck(name=name, status="ok", detail="stub")

    monkeypatch.setattr(health_module, "check_discord_webhook", fake_check)
    resp = client.get("/api/v1/channels/health")
    assert resp.status_code == 200
    checks = resp.json()["checks"]
    assert checks["alert"]["status"] == "ok"
    # URL 平文は返さない
    assert "https://discord.com" not in resp.text


# ---------- P2: Grok メール (IMAP) ----------


def test_grok_mail_get_masks_all_values(client: TestClient) -> None:
    resp = client.get("/api/v1/grok-mail")
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_masked"].endswith("***")
    assert data["password_set"] is True
    assert "old-user@example.com" not in resp.text
    assert "old-password" not in resp.text


def test_grok_mail_save_keeps_blank_fields(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/grok-mail",
        json={"host": "", "port": "", "user": "new-user@example.com", "password": ""},
    )
    assert resp.status_code == 200
    env_text = _env_text()
    assert "IMAP_USER=new-user@example.com" in env_text
    # 空欄フィールドは既存値を維持
    assert "IMAP_PASSWORD=old-password" in env_text


def test_grok_mail_save_rejects_invalid_port(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/grok-mail",
        json={"host": "", "port": "99999", "user": "", "password": ""},
    )
    assert resp.status_code == 400


def test_grok_mail_save_rejects_empty_request(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/grok-mail",
        json={"host": "", "port": "", "user": "", "password": ""},
    )
    assert resp.status_code == 400


def test_grok_mail_health_uses_stubbed_check(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.ui.services import health as health_module

    async def fake_check(
        host: str, port: int, user: str, password: str
    ) -> health_module.HealthCheck:
        return health_module.HealthCheck(name="imap", status="ok", detail="LOGIN OK (stub)")

    monkeypatch.setattr(health_module, "check_imap", fake_check)
    resp = client.get("/api/v1/grok-mail/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------- P3: Ollama URL / システム設定 ----------


def test_ollama_url_save_writes_env(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.ui.services import health as health_module

    async def fake_list(base_url: str) -> list[str]:
        return ["gemma:1b", "gemma:2b"]

    monkeypatch.setattr(health_module, "list_ollama_models", fake_list)
    resp = client.post(
        "/api/v1/model-tiers/ollama-url",
        json={"base_url": "http://host.docker.internal:11434/"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] is True
    # 末尾スラッシュは正規化される
    assert body["ollama_base_url"] == "http://host.docker.internal:11434"
    assert body["model_count"] == 2
    assert "OLLAMA_BASE_URL=http://host.docker.internal:11434" in _env_text()


def test_ollama_url_save_rejects_non_http(client: TestClient) -> None:
    resp = client.post("/api/v1/model-tiers/ollama-url", json={"base_url": "ftp://x"})
    assert resp.status_code == 400
    resp = client.post("/api/v1/model-tiers/ollama-url", json={"base_url": ""})
    assert resp.status_code == 400


def test_config_system_save_writes_env_and_syncs_process_env(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/config/system",
        json={"log_level": "debug", "timezone": "UTC"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["log_level"] == "DEBUG"  # 大文字に正規化
    env_text = _env_text()
    assert "LOG_LEVEL=DEBUG" in env_text
    assert "TIMEZONE=UTC" in env_text
    # 以後に起動する subprocess へ渡るよう process env にも同期される
    assert os.environ["LOG_LEVEL"] == "DEBUG"
    assert os.environ["TIMEZONE"] == "UTC"


def test_config_get_does_not_leak_orphan_secret(client: TestClient) -> None:
    """env_editor 未登録の orphan secret (廃止チャンネルの webhook 等) が GET /config から
    平文漏洩しないこと (2026-07-30 C1 修正の回帰)。旧実装は parsed 全キーを走査し secret 集合
    外を平文で返していたため、公開 readonly instance の /config から漏れていた。"""
    env = Path.cwd() / ".env"
    env.write_text(
        env.read_text(encoding="utf-8")
        + "DISCORD_WEBHOOK_GROK_DAILY=https://discord.com/api/webhooks/9/orphan-secret\n",
        encoding="utf-8",
    )
    resp = client.get("/api/v1/config")
    assert resp.status_code == 200
    # orphan secret の平文がレスポンス全体に一切出ない
    assert "orphan-secret" not in resp.text
    # env_values (displayed のみ) に未登録キー自体が含まれない
    assert "DISCORD_WEBHOOK_GROK_DAILY" not in resp.json()["env_values"]


def test_config_system_save_rejects_bad_values(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/config/system", json={"log_level": "VERBOSE", "timezone": "Asia/Tokyo"}
    )
    assert resp.status_code == 400
    resp = client.post(
        "/api/v1/config/system", json={"log_level": "INFO", "timezone": "Mars/Olympus"}
    )
    assert resp.status_code == 400


# ---------- P4: legacy /health redirect ----------


def test_health_legacy_redirects_to_dashboard(client: TestClient) -> None:
    resp = client.get("/health", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/app/"
