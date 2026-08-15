"""Grok タスク定義 (写し) API のテスト。

- 検証ロジック (id 形式 / 重複 / 上限)
- config_store への版保存 round-trip (tmp SQLite)
- 公開 readonly 面に出さないこと (READ_ONLY_GET_DENYLIST 登録) の回帰固定
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from src.ui.api import grok_tasks as gt
from src.ui.api.grok_tasks import GrokTaskDef, SaveGrokTasksRequest, validate_tasks


def _task(**overrides: str) -> GrokTaskDef:
    base: dict[str, str] = {
        "id": "x_native_signal",
        "name": "X Native Signal",
        "schedule": "Hourly / Every day / All day",
        "window": "直近90分",
        "prompt": "目的: 過去90分の X 投稿を JSONL 収集。",
        "note": "",
        "synced_at": "2026-08-15",
    }
    base.update(overrides)
    return GrokTaskDef(**base)


class TestValidateTasks:
    def test_ok(self) -> None:
        errs = validate_tasks([_task(), _task(id="jp_east_asia")])
        assert errs == []

    def test_rejects_bad_id(self) -> None:
        errs = validate_tasks([_task(id="Bad ID!")])
        assert any("id が不正" in e for e in errs)

    def test_rejects_duplicate_id(self) -> None:
        errs = validate_tasks([_task(), _task()])
        assert any("重複" in e for e in errs)

    def test_rejects_over_max(self) -> None:
        tasks = [_task(id=f"t{i}") for i in range(gt.MAX_TASKS + 1)]
        errs = validate_tasks(tasks)
        assert any("まで" in e for e in errs)

    def test_empty_prompt_rejected_by_model(self) -> None:
        with pytest.raises(ValueError):
            _task(prompt="")


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_save_then_get(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: config_store を tmp SQLite に向ける (db_path seam を monkeypatch)
        db = tmp_path / "config.db"
        from src.storage import config_store

        orig_get, orig_save, orig_hist = (
            config_store.get_config,
            config_store.save_config,
            config_store.list_history,
        )
        monkeypatch.setattr(config_store, "get_config", lambda key, **kw: orig_get(key, db_path=db))
        monkeypatch.setattr(
            config_store,
            "save_config",
            lambda key, value, **kw: orig_save(key, value, note=kw.get("note", ""), db_path=db),
        )
        monkeypatch.setattr(
            config_store,
            "list_history",
            lambda key, **kw: orig_hist(key, limit=kw.get("limit", 50), db_path=db),
        )

        # Act
        req = SaveGrokTasksRequest(tasks=[_task(), _task(id="jp_east_asia")])
        saved = await gt.save_grok_tasks(req)
        out = await gt.get_grok_tasks()

        # Assert
        assert saved["saved"] is True and saved["count"] == 2
        assert [t["id"] for t in out["tasks"]] == ["x_native_signal", "jp_east_asia"]
        assert out["version"] == saved["version"]

    @pytest.mark.asyncio
    async def test_invalid_save_raises_400(self) -> None:
        req = SaveGrokTasksRequest(tasks=[_task(id="dup"), _task(id="dup")])
        with pytest.raises(HTTPException) as ei:
            await gt.save_grok_tasks(req)
        assert ei.value.status_code == 400


class TestExposurePolicy:
    def test_tasks_api_is_denied_on_public_face(self) -> None:
        # 収集関心の詳細 (プロンプト本文) を公開面に出さない (§12 / Tier1 denylist)
        from src.ui.read_only_policy import READ_ONLY_GET_DENYLIST

        assert "/api/v1/grok/tasks" in READ_ONLY_GET_DENYLIST

    def test_config_history_knows_key(self) -> None:
        # 版履歴/revert が config-history 経由で使えること (whitelist 登録)
        from src.ui.api.config_history import _KNOWN_KEYS

        assert gt.CONFIG_KEY in _KNOWN_KEYS
