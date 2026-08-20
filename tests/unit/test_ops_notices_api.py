"""ops_notices API (GET /api/v1/ops-notices) のテスト (2026-08-21)。

ops_notify.post_ops_message の永続化を Web UI「履歴・監査」タブから見るための
read API。運用系の観測データなので公開 instance からは denylist で遮断される
(access_audit と同型)。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.storage.run_history import RunHistoryRepository
from src.ui.read_only_policy import READ_ONLY_GET_DENYLIST, is_read_only_blocked_get
from tests.unit.test_ui_app import _bootstrap_project


@pytest.fixture
def ops_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, RunHistoryRepository]]:
    project = _bootstrap_project(tmp_path)
    monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)

    from src.ui import app as app_module

    fresh_app = app_module.create_app()
    with TestClient(fresh_app) as c:
        yield c, RunHistoryRepository()


def test_ops_notices_returns_newest_first(
    ops_client: tuple[TestClient, RunHistoryRepository],
) -> None:
    client, repo = ops_client
    now = datetime.now(UTC)
    repo.record_ops_notice(
        title="old", body="b1", importance="low", sent=True, when=now - timedelta(hours=2)
    )
    repo.record_ops_notice(title="new", body="b2", importance="high", sent=False, when=now)

    res = client.get("/api/v1/ops-notices")
    assert res.status_code == 200
    data = res.json()
    assert [n["title"] for n in data["notices"]] == ["new", "old"]
    assert data["count"] == 2


def test_ops_notices_limit_is_respected(
    ops_client: tuple[TestClient, RunHistoryRepository],
) -> None:
    client, repo = ops_client
    now = datetime.now(UTC)
    for i in range(5):
        repo.record_ops_notice(
            title=f"t{i}", body="b", importance="low", sent=True, when=now - timedelta(minutes=i)
        )

    res = client.get("/api/v1/ops-notices?limit=2")
    assert res.status_code == 200
    assert len(res.json()["notices"]) == 2


def test_ops_notices_default_limit_is_50(
    ops_client: tuple[TestClient, RunHistoryRepository],
) -> None:
    client, repo = ops_client
    now = datetime.now(UTC)
    for i in range(60):
        repo.record_ops_notice(
            title=f"t{i}", body="b", importance="low", sent=True, when=now - timedelta(minutes=i)
        )

    res = client.get("/api/v1/ops-notices")
    assert res.status_code == 200
    assert len(res.json()["notices"]) == 50


def test_ops_notices_reports_unsent_flag(
    ops_client: tuple[TestClient, RunHistoryRepository],
) -> None:
    client, repo = ops_client
    repo.record_ops_notice(title="fail", body="webhook 未設定", importance="high", sent=False)

    res = client.get("/api/v1/ops-notices")
    notice = res.json()["notices"][0]
    assert notice["sent"] is False
    assert notice["importance"] == "high"
    assert notice["body"] == "webhook 未設定"


def test_ops_notices_empty_when_no_notices(
    ops_client: tuple[TestClient, RunHistoryRepository],
) -> None:
    client, _repo = ops_client
    res = client.get("/api/v1/ops-notices")
    assert res.status_code == 200
    assert res.json() == {"notices": [], "count": 0}


class TestOpsNoticesExposure:
    def test_endpoint_is_denylisted(self) -> None:
        # 運用警告の内容 (title/body) が公開面から読めてはいけない
        assert "/api/v1/ops-notices" in READ_ONLY_GET_DENYLIST
        assert is_read_only_blocked_get("/api/v1/ops-notices") is True
