"""認証の監査証跡テスト (2026-08-02)。

不変量:
- **成功と失敗の両方**が残る (失敗しか記録しないと「使われていない」と「正常稼働」を
  区別できない — 実際にこの誤判定を踏んだ)。
- **email は残さない** (§4)。識別は subject_hash のみ。
- 監査の書き込み失敗で閲覧を止めない (可用性 > 完全性。stdout には必ず出る)。
- 監査証跡自体は公開面に出さない (denylist 対象)。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.storage.run_history import RunHistoryRepository
from src.ui.read_only_policy import READ_ONLY_GET_DENYLIST, is_read_only_blocked_get
from tests.unit.test_read_only_middleware import (
    _VALID_TOKEN,
    _StubVerifier,
    _write_spa_stub,
)
from tests.unit.test_ui_app import _bootstrap_project


@pytest.fixture
def audit_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, RunHistoryRepository]]:
    project = _bootstrap_project(tmp_path)
    _write_spa_stub(project)
    monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)
    # ⚠ READ_ONLY_FLAG は src.ui.app の **import 時** に env から確定する定数。
    # READ_ONLY=1 を立てた状態で初回 import するとセッション全体に漏れ、後続の
    # 通常テストが全部 403 になる (実測)。**env より先に import** して確定させる。
    from src.ui import app as app_module

    monkeypatch.setenv("READ_ONLY", "1")
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "a" * 64)

    from src.ui import read_only_policy

    # 認証済み read の畳み込みは間隔ベース。テスト間で状態を持ち越さない
    read_only_policy._last_auth_audit.clear()
    monkeypatch.setattr(app_module, "READ_ONLY_FLAG", True)
    monkeypatch.setattr(app_module, "AccessVerifier", lambda _c: _StubVerifier())
    with TestClient(app_module.create_app()) as c:
        yield c, RunHistoryRepository()


def _auth(token: str = _VALID_TOKEN) -> dict[str, str]:
    return {
        "Cf-Access-Jwt-Assertion": token,
        "Cf-Connecting-Ip": "203.0.113.9",
        "Cf-Ipcountry": "JP",
    }


class TestAuditRecording:
    def test_successful_authentication_is_recorded(
        self, audit_client: tuple[TestClient, RunHistoryRepository]
    ) -> None:
        client, repo = audit_client
        assert client.get("/api/v1/jobs", headers=_auth()).status_code == 200

        events = repo.list_access_audit()
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "authenticated"
        assert ev["subject_hash"]  # 識別子はある
        assert ev["client_ip"] == "203.0.113.9"
        assert ev["country"] == "JP"

    def test_rejected_credential_is_recorded(
        self, audit_client: tuple[TestClient, RunHistoryRepository]
    ) -> None:
        client, repo = audit_client
        assert client.get("/api/v1/jobs", headers=_auth("forged")).status_code == 403

        events = repo.list_access_audit()
        assert [e["event"] for e in events] == ["rejected"]
        assert events[0]["subject_hash"] == ""  # 検証できていないので身元は空
        assert events[0]["detail"] == "invalid_or_expired_token"

    def test_anonymous_access_is_not_recorded(
        self, audit_client: tuple[TestClient, RunHistoryRepository]
    ) -> None:
        # 資格情報を提示していない匿名アクセスは「拒否」ではない。
        # 公開 URL には bot が来るため記録すると証跡が埋もれる。
        client, repo = audit_client
        client.get("/api/v1/jobs")
        client.get("/api/v1/history")
        assert repo.list_access_audit() == []

    def test_authenticated_reads_are_collapsed(
        self, audit_client: tuple[TestClient, RunHistoryRepository]
    ) -> None:
        # 1 画面が多数の API を呼ぶため、同一 subject の read は畳んで記録する
        client, repo = audit_client
        for _ in range(5):
            client.get("/api/v1/jobs", headers=_auth())
        assert len(repo.list_access_audit()) == 1

    def test_email_is_never_persisted(
        self, audit_client: tuple[TestClient, RunHistoryRepository]
    ) -> None:
        client, repo = audit_client
        client.get("/api/v1/jobs", headers=_auth())
        # _StubVerifier の Identity は user@example.com を持つ (§4: 保存してはならない)
        blob = str(repo.list_access_audit())
        assert "user@example.com" not in blob
        assert "@" not in blob

    def test_audit_persist_failure_does_not_block_access(
        self, audit_client: tuple[TestClient, RunHistoryRepository], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _repo = audit_client

        def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(RunHistoryRepository, "record_access_audit", boom)
        # 監査が書けなくても閲覧そのものは通る (stdout には残る)
        assert client.get("/api/v1/jobs", headers=_auth()).status_code == 200


class TestAuditExposure:
    def test_audit_endpoint_is_denylisted(self) -> None:
        # 監査証跡自体が公開面から読めてはいけない
        assert "/api/v1/access-audit" in READ_ONLY_GET_DENYLIST
        assert is_read_only_blocked_get("/api/v1/access-audit") is True

    def test_anonymous_cannot_read_audit(
        self, audit_client: tuple[TestClient, RunHistoryRepository]
    ) -> None:
        client, _ = audit_client
        assert client.get("/api/v1/access-audit").status_code == 403

    def test_authenticated_can_read_audit(
        self, audit_client: tuple[TestClient, RunHistoryRepository]
    ) -> None:
        client, _ = audit_client
        res = client.get("/api/v1/access-audit", headers=_auth())
        assert res.status_code == 200
        assert "events" in res.json()


class TestRetention:
    def test_purge_keeps_recent_and_drops_old(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime, timedelta

        repo = RunHistoryRepository(db_path=tmp_path / "audit.db")
        now = datetime.now(UTC)
        repo.record_access_audit(event="authenticated", subject_hash="recent", when=now)
        repo.record_access_audit(event="rejected", subject_hash="", when=now - timedelta(days=200))
        assert len(repo.list_access_audit()) == 2

        # 180 日 retention: 古い方だけ落ちる
        assert repo.purge_old_access_audit(days=180) == 1
        remaining = repo.list_access_audit()
        assert [e["subject_hash"] for e in remaining] == ["recent"]
