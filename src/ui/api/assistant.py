"""分析チャット API (2026-07-12)。

POST /api/v1/assistant/chat — 自然言語の質問 → データツール実行 → 接地回答。
READ_ONLY instance (公開 mobile) は write-block middleware が POST を 403 で落とすため、
本エンドポイントは 127.0.0.1 の full instance でのみ使える (LLM 濫用防止も兼ねる)。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger

_log = get_logger(__name__)

assistant_api = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

_MESSAGE_MAX = 2000
_HISTORY_MAX = 8


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str = "user"
    content: str = ""


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str
    history: list[ChatTurn] = Field(default_factory=list)
    # 会話単位のモデル override (空 = dialog ティアの既定)。denylist / fallback は
    # build_llm_for_ref が既定経路と同一に適用する。
    model: str = ""


@assistant_api.post("/chat")
async def assistant_chat(request: Request, body: ChatBody) -> dict[str, Any]:
    """1 turn のチャット。ツール実行内訳 + 接地回答を返す。"""
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message が空です")
    if len(message) > _MESSAGE_MAX:
        raise HTTPException(status_code=400, detail=f"message は {_MESSAGE_MAX} 字以内")

    from src.assistant.orchestrator import chat_turn
    from src.assistant.tools import ToolContext
    from src.config_loader import load_app_config
    from src.tools.llm_client import LLMForbiddenModelError
    from src.tools.model_tiers import Step, build_llm_for, build_llm_for_ref
    from src.ui.api.articles_feed import _resolve_embedder

    repo = request.app.state.repo
    # model override は readonly instance でも許可 (2026-07-19 ユーザー判断)。
    # readonly の防御境界は URL 秘匿 + write 全遮断であり、チャット開放済みの下で
    # モデル固定だけ残す意味は薄い。denylist (禁止系 400) は従来どおり適用される。
    model_ref = body.model.strip()
    try:
        if model_ref:
            llm = build_llm_for_ref(model_ref, Step.ASSISTANT_CHAT, load_app_config())
        else:
            llm = build_llm_for(Step.ASSISTANT_CHAT, load_app_config())
    except LLMForbiddenModelError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 — LLM 構築失敗はユーザー向けに 503
        _log.error("assistant_llm_init_failed", error=str(e))
        raise HTTPException(status_code=503, detail="LLM を初期化できません") from e

    ctx = ToolContext(repo=repo, embedder=_resolve_embedder(request), db_path=repo.db_path)
    history = [{"role": t.role, "content": t.content} for t in body.history[-_HISTORY_MAX:]]
    result = await chat_turn(llm=llm, ctx=ctx, message=message, history=history)
    return {"answer": result.answer, "tools": result.tools, "model": result.model}
