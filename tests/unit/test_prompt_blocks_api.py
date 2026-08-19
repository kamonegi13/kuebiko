"""block 方式プロンプト API (/api/v1/prompts/{id}/blocks) の unit test。

summarizer 用 test_prompt_rubric_api と同じ関心: 保存経路自身が最後の砦 (render 検証)
を持つこと・壊れた編集を 400 で拒否すること・版保存と cache 破棄が起きること。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.prompts.prompt_store import invalidate_prompt_cache
from src.storage import config_store
from src.ui.api.prompt_blocks import prompt_blocks_api

_PROMPT_ID = "status_synthesis"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # config_store を一時 SQLite に向ける (production PG に触れない)
    db = tmp_path / "config.db"
    monkeypatch.setattr(config_store, "_open", lambda db_path=None: _open_tmp(db))
    invalidate_prompt_cache()
    app = FastAPI()
    app.include_router(prompt_blocks_api)
    with TestClient(app) as c:
        yield c
    invalidate_prompt_cache()


def _open_tmp(db: Path) -> object:
    import sqlite3

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(config_store._DDL)  # noqa: SLF001 — test 用の直結
    return conn


class TestGet:
    def test_returns_seed_blocks_with_slots_in_skeleton_order(self, client: TestClient) -> None:
        # Act
        res = client.get(f"/api/v1/prompts/{_PROMPT_ID}/blocks")

        # Assert: seed が読め、slot 順が skeleton の出現順で返る
        assert res.status_code == 200
        data = res.json()
        assert data["slots"][0] == "mission"
        assert data["slots"][-1] == "completeness"
        assert {s["field_id"] for s in data["rubric"]["sections"]} == set(data["slots"])
        assert data["runtime"]["active_source"] == "composed"

    def test_unknown_prompt_is_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/prompts/no_such/blocks").status_code == 404

    def test_field_rubric_prompt_is_not_served_here(self, client: TestClient) -> None:
        # summarizer は専用 API (prompt_rubric) の持ち場 — こちらでは 404
        assert client.get("/api/v1/prompts/summarizer/blocks").status_code == 404

    def test_managed_lists_both_kinds(self, client: TestClient) -> None:
        res = client.get("/api/v1/prompts/managed")
        assert res.status_code == 200
        ids = {p["prompt_id"] for p in res.json()["prompts"]}
        assert {"summarizer", _PROMPT_ID} <= ids


class TestPut:
    def _rubric(self, client: TestClient) -> dict[str, Any]:
        payload: dict[str, Any] = client.get(f"/api/v1/prompts/{_PROMPT_ID}/blocks").json()
        return dict(payload["rubric"])

    def test_valid_edit_is_saved_with_version(self, client: TestClient) -> None:
        # Arrange: mission block を書き換える
        rubric = self._rubric(client)
        for s in rubric["sections"]:
            if s["field_id"] == "mission":
                s["body"] = "編集後のミッション文 (テスト)"

        # Act
        res = client.put(
            f"/api/v1/prompts/{_PROMPT_ID}/blocks",
            json={"rubric": rubric, "note": "test"},
        )

        # Assert: 版保存され、GET が新しい本文を返す
        assert res.status_code == 200
        assert res.json()["saved"] is True
        got = self._rubric(client)
        assert any("編集後のミッション文" in s["body"] for s in got["sections"])

    def test_missing_block_is_rejected_400(self, client: TestClient) -> None:
        # Arrange: mission block を削除 (slot が埋まらない = 段落欠落)
        rubric = self._rubric(client)
        rubric["sections"] = [s for s in rubric["sections"] if s["field_id"] != "mission"]

        # Act / Assert
        res = client.put(f"/api/v1/prompts/{_PROMPT_ID}/blocks", json={"rubric": rubric})
        assert res.status_code == 400
        assert "mission" in res.json()["detail"]

    def test_broken_jinja_is_rejected_by_the_render_gate(self, client: TestClient) -> None:
        """保存経路自身の最後の砦 — 壊れた Jinja が「保存成功」にならない (WP2 の教訓)。"""
        rubric = self._rubric(client)
        for s in rubric["sections"]:
            if s["field_id"] == "mission":
                s["body"] = "{% if broken %}閉じない"

        res = client.put(f"/api/v1/prompts/{_PROMPT_ID}/blocks", json={"rubric": rubric})
        assert res.status_code == 400
        assert "Jinja 構文エラー" in res.json()["detail"]


class TestPreview:
    def test_preview_returns_composed_and_rendered_sample(self, client: TestClient) -> None:
        res = client.post(f"/api/v1/prompts/{_PROMPT_ID}/blocks/preview", json={})
        assert res.status_code == 200
        data = res.json()
        assert data["valid"] is True
        assert "純粋な JSON のみ出力。" in data["composed"]
        # サンプル context のデータが実際に展開されている (render の実証)
        assert "サンプル PIR" in data["rendered_sample"]
