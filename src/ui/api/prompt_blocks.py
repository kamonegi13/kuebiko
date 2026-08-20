"""block 方式プロンプト (status_synthesis 以降) の REST API (層分けの一般化、2026-08-20)。

summarizer 専用の ``prompt_rubric.py`` と同じ責務の spec 汎用版。**新しい path prefix は
作らない** — ``/api/v1/prompts`` 配下に置くことで ``READ_ONLY_GET_DENYLIST`` の前方一致
遮断を自動継承する (CLAUDE.md §12 I9)。保存は config_store のみ (.j2 へ書き戻さない、I8)。

endpoint:
- GET  /api/v1/prompts/managed                     … 管理対象プロンプトの一覧 (UI メニュー)
- GET  /api/v1/prompts/{prompt_id}/blocks          … 現行 blocks + slots + runtime
- PUT  /api/v1/prompts/{prompt_id}/blocks          … 検証して版保存 (エラーは 400 で拒否)
- POST /api/v1/prompts/{prompt_id}/blocks/preview  … 検証 + 合成原文 + サンプル render
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger
from src.prompts.block_composer import compose_blocks, skeleton_slots
from src.prompts.prompt_store import (
    build_prompt_template,
    fallback_reason,
    invalidate_prompt_cache,
    is_prompt_composer_enabled,
    load_prompt_rubric,
    load_skeleton,
    validate_block_rubric,
)
from src.prompts.registry import PromptSpec, all_specs, get_spec
from src.prompts.rubric_model import SummarizerRubric
from src.prompts.sample_context import sample_context_for
from src.storage import config_store

_log = get_logger(__name__)

prompt_blocks_api = APIRouter(prefix="/api/v1/prompts", tags=["prompts"])

_PREVIEW_CHAR_CAP = 80_000
_MAX_ERRORS_IN_DETAIL = 10


def _spec_or_404(prompt_id: str) -> PromptSpec:
    spec = get_spec(prompt_id)
    if spec is None or spec.kind != "block":
        raise HTTPException(
            status_code=404, detail=f"管理対象の block プロンプトではありません: {prompt_id}"
        )
    return spec


def _runtime_payload(spec: PromptSpec) -> dict[str, Any]:
    """build を実際に呼んで判定する (表示と実体を一致させる — summarizer と同じ作法)。"""
    template = build_prompt_template(spec, spec.legacy_path)
    history = config_store.list_history(spec.config_key, limit=1)
    return {
        "composer_enabled": is_prompt_composer_enabled(spec),
        "active_source": "composed" if template is not None else "legacy_file",
        "fallback_reason": fallback_reason(spec.prompt_id),
        "legacy_path": str(spec.legacy_path),
        "env_flag": spec.env_flag,
        "version": history[0].version if history else None,
        "saved_at": history[0].created_at if history else "",
    }


@prompt_blocks_api.get("/managed")
async def managed_prompts() -> dict[str, Any]:
    """管理対象プロンプトの一覧 (summarizer 含む — UI のプロンプト選択メニュー用)。"""
    items: list[dict[str, Any]] = []
    for spec in all_specs():
        history = config_store.list_history(spec.config_key, limit=1)
        items.append(
            {
                "prompt_id": spec.prompt_id,
                "title": spec.title,
                "kind": spec.kind,
                "config_key": spec.config_key,
                # UI が raw ファイル一覧の legacy .j2 と突合し「rollback 用の据置 —
                # 編集はカードから」のバナーを出すための対応付け。
                "legacy_path": str(spec.legacy_path),
                "version": history[0].version if history else None,
                "saved_at": history[0].created_at if history else "",
            }
        )
    return {"prompts": items}


@prompt_blocks_api.get("/{prompt_id}/blocks")
async def get_blocks(prompt_id: str) -> dict[str, Any]:
    """現行 blocks (編集層) + skeleton slots (順序) + runtime 状態を返す。"""
    spec = _spec_or_404(prompt_id)
    rubric = load_prompt_rubric(spec)
    if rubric is None:
        raise HTTPException(status_code=500, detail="編集層が見つかりません (seed 未投入)")
    skeleton = load_skeleton(spec)
    slots = skeleton_slots(skeleton) if skeleton else []
    return {
        "prompt_id": spec.prompt_id,
        "title": spec.title,
        "rubric": rubric.model_dump(),
        # slot 順 = プロンプト内の出現順。UI はこの順でカードを描く (DB の配列順でなく)。
        "slots": slots,
        "runtime": _runtime_payload(spec),
    }


class SaveBlocksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rubric: SummarizerRubric
    note: str = Field(default="", max_length=200)


@prompt_blocks_api.put("/{prompt_id}/blocks")
async def put_blocks(prompt_id: str, req: SaveBlocksRequest) -> dict[str, Any]:
    """blocks を検証して DB に版保存する。エラーがあれば 400 で保存を拒否する。

    検証は slot/block 1:1 → Jinja 構文 → サンプル context での render まで
    (保存経路自身に最後の砦を置く — summarizer WP2 の教訓)。
    """
    spec = _spec_or_404(prompt_id)
    errors = validate_block_rubric(spec, req.rubric)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors[:_MAX_ERRORS_IN_DETAIL]))
    version = config_store.save_config(
        spec.config_key,
        req.rubric.model_dump(),
        note=req.note.strip() or "UI 編集",
    )
    invalidate_prompt_cache(spec.prompt_id)
    _log.info(
        "prompt_blocks_saved",
        prompt=spec.prompt_id,
        version=version,
        block_count=len(req.rubric.sections),
    )
    return {"saved": True, "version": version}


class PreviewBlocksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rubric: SummarizerRubric | None = None


@prompt_blocks_api.post("/{prompt_id}/blocks/preview")
async def preview_blocks(prompt_id: str, req: PreviewBlocksRequest) -> dict[str, Any]:
    """検証結果 + 合成原文 + サンプル context での render 結果を返す (常に 200)。"""
    spec = _spec_or_404(prompt_id)
    rubric = req.rubric or load_prompt_rubric(spec)
    if rubric is None:
        raise HTTPException(status_code=500, detail="編集層が見つかりません (seed 未投入)")
    errors = validate_block_rubric(spec, rubric)
    skeleton = load_skeleton(spec)
    composed = compose_blocks(skeleton, rubric) if skeleton and not errors else ""
    rendered_sample = ""
    if not errors and composed:
        from src.prompts.prompt_store import build_prompt_environment

        context = sample_context_for(spec.prompt_id) or {}
        # 失敗理由は errors 側の検証 (validate_block_rubric) が持つため握って空にする
        with contextlib.suppress(Exception):
            rendered_sample = (
                build_prompt_environment(spec, spec.legacy_path)
                .from_string(composed)
                .render(**context)
            )
    return {
        "valid": not errors,
        "errors": errors,
        "composed": composed[:_PREVIEW_CHAR_CAP],
        "composed_chars": len(composed),
        "rendered_sample": rendered_sample[:_PREVIEW_CHAR_CAP],
    }
