"""Grok タスク定義 (外部 Grok 側スケジュールタスクの写し) の管理 API。

Grok のスケジュールタスク (プロンプト・実行頻度・対象窓) は Grok 側 UI にしか
存在せず、アカウント事故や誤編集で収集設計そのものを失うリスクがある。
本 API はその**参照写し**を DB (config_store, key=grok_tasks) に版保存する。

重要な前提 (UI にも明示する):
- **SSoT は Grok 側**。kuebiko から Grok へ push はできず、この写しは記録にすぎない。
  乖離を防ぐため、利用者が Grok 側へ反映した日時を ``synced_at`` に自ら記録する。
- kuebiko の実行系はこの写しを一切消費しない (theme routing は
  ``src/grok/jsonl_to_briefings.py`` が受け側として持つ)。

セキュリティ:
- 収集関心の詳細 (プロンプト本文) を含むため、公開 readonly 面には出さない
  (``READ_ONLY_GET_DENYLIST`` に ``/api/v1/grok/tasks`` を登録)。
- write は READ_ONLY middleware が 403 で block (個別ガード不要)。
- 版履歴/revert は config-history (key=grok_tasks) に乗る。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger

grok_tasks_api = APIRouter(prefix="/api/v1/grok/tasks", tags=["grok"])

_log = get_logger(__name__)

CONFIG_KEY = "grok_tasks"
MAX_TASKS = 20
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class GrokTaskDef(BaseModel):
    """Grok 側スケジュールタスク 1 本の写し。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    schedule: str = Field(default="", max_length=200)  # 例: "Hourly / Every day / All day"
    window: str = Field(default="", max_length=100)  # 例: "直近90分"
    prompt: str = Field(min_length=1, max_length=30000)
    note: str = Field(default="", max_length=2000)
    synced_at: str = Field(default="", max_length=50)  # Grok 側へ反映した日 (利用者記入)


class SaveGrokTasksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[GrokTaskDef]


def validate_tasks(tasks: list[GrokTaskDef]) -> list[str]:
    """保存前検証。エラー文のリストを返す (空 = OK)。"""
    errs: list[str] = []
    if len(tasks) > MAX_TASKS:
        errs.append(f"タスクは {MAX_TASKS} 件までです")
    seen: set[str] = set()
    for t in tasks:
        if not _ID_RE.match(t.id):
            errs.append(f"id が不正です (英小文字/数字/-/_ のみ): {t.id!r}")
        if t.id in seen:
            errs.append(f"id が重複しています: {t.id!r}")
        seen.add(t.id)
    return errs


def _latest_meta() -> dict[str, Any]:
    """現行版のメタ (version / 保存日時)。未保存なら空 dict。"""
    from src.storage.config_store import list_history

    history = list_history(CONFIG_KEY, limit=1)
    if not history:
        return {}
    return {"version": history[0].version, "saved_at": history[0].created_at}


@grok_tasks_api.get("")
async def get_grok_tasks() -> dict[str, Any]:
    """タスク定義の写し一覧 (UI 表示/編集用)。"""
    from src.storage.config_store import get_config

    value = get_config(CONFIG_KEY)
    tasks = value.get("tasks", []) if isinstance(value, dict) else []
    return {"tasks": tasks, **_latest_meta()}


@grok_tasks_api.put("")
async def save_grok_tasks(req: SaveGrokTasksRequest) -> dict[str, Any]:
    """タスク定義の写しを検証して版保存する。"""
    errs = validate_tasks(req.tasks)
    if errs:
        raise HTTPException(status_code=400, detail="; ".join(errs))
    from src.storage.config_store import save_config

    value = {"tasks": [t.model_dump() for t in req.tasks]}
    version = save_config(CONFIG_KEY, value, note="UI 保存 (Grok タスク定義の写し)")
    _log.info("grok_tasks_saved", count=len(req.tasks), version=version)
    return {"saved": True, "count": len(req.tasks), "version": version}
