"""ニュースの絞り込みビュー API (``/api/v1/news-views``) のテスト。

docs/news_filter_ux.md §3-4: 既定ビューは frontend 側の定数で持つため、
この API は **利用者保存ビューのみ** を CRUD (全量保存) する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.ui.api import news_views as nv


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(nv, "_DB_PATH", tmp_path / "news_views.db")
    app = FastAPI()
    app.include_router(nv.news_views_api)
    return TestClient(app)


def test_empty_by_default(client: TestClient) -> None:
    r = client.get("/api/v1/news-views")
    assert r.status_code == 200
    assert r.json() == {"views": []}


def test_save_and_load_roundtrip(client: TestClient) -> None:
    payload = {
        "views": [
            {
                "id": "my-vuln-watch",
                "label": "自分の脆弱性ウォッチ",
                "filters": {"category": "vuln", "min_severity": "S2"},
            },
        ],
    }
    r = client.put("/api/v1/news-views", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["count"] == 1

    r2 = client.get("/api/v1/news-views")
    views = r2.json()["views"]
    assert len(views) == 1
    assert views[0]["id"] == "my-vuln-watch"
    assert views[0]["label"] == "自分の脆弱性ウォッチ"
    assert views[0]["filters"]["category"] == "vuln"
    assert views[0]["filters"]["min_severity"] == "S2"


def test_save_overwrites_full_list_rename_and_delete(client: TestClient) -> None:
    client.put(
        "/api/v1/news-views",
        json={
            "views": [
                {"id": "a", "label": "A", "filters": {}},
                {"id": "b", "label": "B", "filters": {}},
            ],
        },
    )
    # rename "a" → "A2" and drop "b" (次の保存は常に全件を送り直す)
    r = client.put(
        "/api/v1/news-views",
        json={"views": [{"id": "a", "label": "A2", "filters": {}}]},
    )
    assert r.status_code == 200
    views = client.get("/api/v1/news-views").json()["views"]
    assert [v["id"] for v in views] == ["a"]
    assert views[0]["label"] == "A2"


def test_duplicate_ids_rejected(client: TestClient) -> None:
    r = client.put(
        "/api/v1/news-views",
        json={
            "views": [
                {"id": "a", "label": "A", "filters": {}},
                {"id": "a", "label": "A again", "filters": {}},
            ],
        },
    )
    assert r.status_code == 400


def test_unknown_filter_field_rejected(client: TestClient) -> None:
    r = client.put(
        "/api/v1/news-views",
        json={"views": [{"id": "a", "label": "A", "filters": {"bogus_field": "x"}}]},
    )
    assert r.status_code == 422


def test_version_history_accumulates(client: TestClient) -> None:
    from src.storage.config_store import list_history

    client.put("/api/v1/news-views", json={"views": [{"id": "a", "label": "A", "filters": {}}]})
    client.put("/api/v1/news-views", json={"views": [{"id": "a", "label": "A2", "filters": {}}]})
    history = list_history(nv._CONFIG_KEY, db_path=nv._DB_PATH)
    assert [h.version for h in history] == [2, 1]


def test_registered_in_config_history_known_keys() -> None:
    from src.ui.api.config_history import _KNOWN_KEYS

    assert "news_views" in _KNOWN_KEYS


def test_not_in_remote_write_allowlist() -> None:
    """遠隔 write は意図的に許可しない (write 先は DB だが利用者判断で未登録)。"""
    from src.ui.read_only_policy import is_remote_writable

    assert is_remote_writable("/api/v1/news-views") is False
