"""python_block kind (ioc_llm_verifier) の /api/v1/prompts/{id}/blocks API test。

test_prompt_blocks_api.py (block kind = status_synthesis) と同じ fixture / 関心を、
skeleton を持たない python_block kind に対して確認する:
- slots は対象モジュールの固定表 (SLOT_IDS) の順で返る (skeleton マーカーではない)
- 保存前検証はサンプル候補 (SAMPLE_TARGETS) での実合成まで通す
- preview は Jinja render 段が無く composed == rendered_sample
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.cti.ioc_llm_verifier import SLOT_IDS
from src.prompts.prompt_store import invalidate_prompt_cache
from src.storage import config_store
from src.ui.api.prompt_blocks import prompt_blocks_api

_PROMPT_ID = "ioc_llm_verifier"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
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
    def test_returns_seed_blocks_with_slots_from_slot_ids(self, client: TestClient) -> None:
        res = client.get(f"/api/v1/prompts/{_PROMPT_ID}/blocks")
        assert res.status_code == 200
        data = res.json()
        assert data["kind"] == "python_block"
        assert data["slots"] == list(SLOT_IDS)
        assert {s["field_id"] for s in data["rubric"]["sections"]} == set(data["slots"])
        assert data["runtime"]["active_source"] == "composed"

    def test_managed_lists_python_block_prompt(self, client: TestClient) -> None:
        res = client.get("/api/v1/prompts/managed")
        assert res.status_code == 200
        items = {p["prompt_id"]: p for p in res.json()["prompts"]}
        assert _PROMPT_ID in items
        assert items[_PROMPT_ID]["kind"] == "python_block"
        assert items[_PROMPT_ID]["legacy_path"] == "src/cti/ioc_llm_verifier.py"


class TestPut:
    def _rubric(self, client: TestClient) -> dict[str, Any]:
        payload: dict[str, Any] = client.get(f"/api/v1/prompts/{_PROMPT_ID}/blocks").json()
        return dict(payload["rubric"])

    def test_valid_edit_is_saved_with_version(self, client: TestClient) -> None:
        rubric = self._rubric(client)
        for s in rubric["sections"]:
            if s["field_id"] == "rules":
                s["body"] = "編集後のルール文 (テスト)"

        res = client.put(
            f"/api/v1/prompts/{_PROMPT_ID}/blocks",
            json={"rubric": rubric, "note": "test"},
        )

        assert res.status_code == 200
        assert res.json()["saved"] is True
        got = self._rubric(client)
        assert any("編集後のルール文" in s["body"] for s in got["sections"])

    def test_missing_block_is_rejected_400(self, client: TestClient) -> None:
        rubric = self._rubric(client)
        rubric["sections"] = [s for s in rubric["sections"] if s["field_id"] != "criteria"]

        res = client.put(f"/api/v1/prompts/{_PROMPT_ID}/blocks", json={"rubric": rubric})
        assert res.status_code == 400
        assert "criteria" in res.json()["detail"]

    def test_duplicate_block_id_is_rejected_400(self, client: TestClient) -> None:
        rubric = self._rubric(client)
        rubric["sections"] = [*rubric["sections"], rubric["sections"][0]]

        res = client.put(f"/api/v1/prompts/{_PROMPT_ID}/blocks", json={"rubric": rubric})
        assert res.status_code == 400
        assert "重複" in res.json()["detail"]

    def test_examples_are_rejected_400(self, client: TestClient) -> None:
        rubric = self._rubric(client)
        rubric["examples"] = [{"label": "x", "json_text": "{}"}]

        res = client.put(f"/api/v1/prompts/{_PROMPT_ID}/blocks", json={"rubric": rubric})
        assert res.status_code == 400
        assert "examples" in res.json()["detail"]


class TestPreview:
    def test_preview_returns_composed_equal_to_rendered_sample(self, client: TestClient) -> None:
        res = client.post(f"/api/v1/prompts/{_PROMPT_ID}/blocks/preview", json={})
        assert res.status_code == 200
        data = res.json()
        assert data["valid"] is True
        # python_block に Jinja render 段は無い — サンプル候補での合成結果がそのまま両方に載る
        assert data["composed"] == data["rendered_sample"]
        assert "## 候補リスト" in data["composed"]
        assert "0. type=" in data["composed"]  # SAMPLE_TARGETS がプレビューに反映されている

    def test_preview_of_broken_draft_reports_errors_without_crashing(
        self, client: TestClient
    ) -> None:
        rubric: dict[str, Any] = client.get(f"/api/v1/prompts/{_PROMPT_ID}/blocks").json()["rubric"]
        rubric["sections"] = [s for s in rubric["sections"] if s["field_id"] != "intro"]

        res = client.post(f"/api/v1/prompts/{_PROMPT_ID}/blocks/preview", json={"rubric": rubric})
        assert res.status_code == 200
        data = res.json()
        assert data["valid"] is False
        assert any("intro" in e for e in data["errors"])
        assert data["composed"] == ""
