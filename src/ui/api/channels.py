"""チャンネル管理 API (チャンネル config-driven 化 C3 + 設定・死活の画面統合 P1)。

GET  /api/v1/channels                 レジストリ + built-in 集合 + rule 参照数 + webhook 設定状況
POST /api/v1/channels                 検証して DB (config_store) に版保存 + キャッシュ無効化
GET  /api/v1/channels/health          チャンネル別 webhook 疎通 (GET 検証・投稿なし)
POST /api/v1/channels/{id}/webhook    投稿先 URL を .env に保存/削除 (即時反映)

write は READ_ONLY middleware (app.py) が 403 で block するため、ここで個別ガードは不要。
webhook URL の平文はレスポンスに含めない (マスク値のみ。§4/§12)。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger

channels_api = APIRouter(prefix="/api/v1/channels", tags=["channels"])

_log = get_logger(__name__)


class SaveChannelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channels: list[dict[str, Any]]


def _rule_referenced_channels() -> set[str]:
    """現行 routing rules が参照している channel id 集合。"""
    from src.cti.routing_rules import load_routing_rules

    rules = load_routing_rules(force_reload=True)
    return {str(r.get("channel")) for r in rules if r.get("channel")}


def _webhook_set_map() -> dict[str, bool]:
    """channel id → webhook URL が設定済みか (URL 自体は返さない)。"""
    from src.config_loader import load_app_config
    from src.tools.channel_registry import resolve_webhooks

    try:
        config = load_app_config()
        return {ch: bool(url) for ch, url in resolve_webhooks(config.discord_webhooks).items()}
    except Exception as e:  # noqa: BLE001 — 表示補助のため失敗しても一覧自体は返す
        _log.warning("channels_webhook_status_failed", error=str(e))
        return {}


def _read_env_values(request: Request) -> dict[str, str]:
    """UI が編集対象とする ``.env`` の現在値 (os.environ を優先、pydantic-settings と同順)。"""
    from src.ui.services.env_editor import parse_env

    editor = request.app.state.file_editor
    env_path = editor.env_path()
    parsed = parse_env(env_path.read_text(encoding="utf-8")) if env_path.exists() else {}
    return parsed


def _webhook_masked_map(request: Request) -> dict[str, str]:
    """channel id → マスク済み webhook URL (無効チャンネル含む全件。平文は返さない)。"""
    from src.tools.channel_registry import load_channels
    from src.ui.services.env_editor import mask_value

    try:
        env_values = _read_env_values(request)
        out: dict[str, str] = {}
        for ch in load_channels():
            value = os.environ.get(ch.webhook_env_key) or env_values.get(ch.webhook_env_key, "")
            out[ch.id] = mask_value(value)
        return out
    except Exception as e:  # noqa: BLE001 — 表示補助のため失敗しても一覧自体は返す
        _log.warning("channels_webhook_masked_failed", error=str(e))
        return {}


@channels_api.get("")
async def get_channels(request: Request) -> dict[str, Any]:
    """チャンネルレジストリ一覧 (UI 編集用)。"""
    from dataclasses import asdict

    from src.tools.channel_registry import (
        BUILTIN_CHANNELS,
        invalidate_channels_cache,
        load_channels,
    )

    # UI は常に最新の DB 値を見る (キャッシュは pipeline 用)
    invalidate_channels_cache()
    channels = load_channels()
    rule_refs = _rule_referenced_channels()
    return {
        "channels": [asdict(c) for c in channels],
        "builtin_ids": [c.id for c in BUILTIN_CHANNELS],
        "rule_refs": {c.id: (c.id in rule_refs) for c in channels},
        "webhook_set": _webhook_set_map(),
        "webhook_masked": _webhook_masked_map(request),
    }


@channels_api.get("/health")
async def channels_health() -> dict[str, Any]:
    """有効チャンネルの webhook 疎通 (GET 検証・実投稿なし)。URL は返さない。"""
    import asyncio

    from src.config_loader import load_app_config
    from src.tools.channel_registry import resolve_webhooks
    from src.ui.services.health import check_discord_webhook

    try:
        config = load_app_config()
        resolved = resolve_webhooks(config.discord_webhooks)
    except Exception as e:  # noqa: BLE001 — 疎通表示の失敗で画面を壊さない
        _log.warning("channels_health_resolve_failed", error=str(e))
        return {"checks": {}, "error": str(e)}

    results = await asyncio.gather(
        *(check_discord_webhook(ch_id, url) for ch_id, url in resolved.items()),
        return_exceptions=True,
    )
    checks: dict[str, dict[str, str]] = {}
    for (ch_id, _), result in zip(resolved.items(), results, strict=True):
        if isinstance(result, BaseException):
            checks[ch_id] = {"status": "error", "detail": str(result)}
        else:
            checks[ch_id] = {"status": result.status, "detail": result.detail}
    return {"checks": checks, "error": None}


# Discord webhook URL として受け付ける prefix (canary/ptb ドメイン含む)。
_WEBHOOK_URL_PREFIXES: tuple[str, ...] = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
    "https://canary.discord.com/api/webhooks/",
    "https://ptb.discord.com/api/webhooks/",
)


class SaveWebhookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str


@channels_api.post("/{channel_id}/webhook")
async def save_channel_webhook(
    channel_id: str, request: Request, req: SaveWebhookRequest
) -> dict[str, Any]:
    """チャンネルの投稿先 webhook URL を .env に保存する (空文字 = 削除、即時反映)。

    保存先はチャンネル定義の ``webhook_env_key`` (.env)。DB (config_store) には置かない —
    版履歴 + 日次 pg_dump backup に秘密が残留するため (§4、anthropic-key と同方針)。
    """
    from src.tools.channel_registry import invalidate_channels_cache, load_channels
    from src.ui.services.env_editor import EnvEditError, mask_value, update_env

    invalidate_channels_cache()
    channel = next((c for c in load_channels() if c.id == channel_id), None)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"チャンネル {channel_id} は存在しません")

    url = req.url.strip()
    if url and not url.startswith(_WEBHOOK_URL_PREFIXES):
        raise HTTPException(
            status_code=400,
            detail=(
                "Discord webhook URL の形式ではありません "
                "(https://discord.com/api/webhooks/… を貼り付けてください)"
            ),
        )

    editor = request.app.state.file_editor
    try:
        update_env(editor.env_path(), {channel.webhook_env_key: url})
    except EnvEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _log.info("channel_webhook_saved", channel=channel_id, set=bool(url))
    return {
        "saved": True,
        "webhook_set": bool(url),
        "webhook_masked": mask_value(url),
    }


@channels_api.post("")
async def save_channels(req: SaveChannelsRequest) -> dict[str, Any]:
    """チャンネルレジストリを検証して保存 (版履歴は config_store に残る)。"""
    from src.storage.config_store import save_config
    from src.tools.channel_registry import (
        CHANNELS_CONFIG_KEY,
        invalidate_channels_cache,
        validate_channels,
    )

    errs = validate_channels(req.channels, rule_channels=_rule_referenced_channels())
    if errs:
        raise HTTPException(status_code=400, detail="; ".join(errs))

    version = save_config(CHANNELS_CONFIG_KEY, req.channels, note="UI 保存 (チャンネル管理)")
    invalidate_channels_cache()
    _log.info("channels_saved", count=len(req.channels), version=version)
    return {"saved": True, "count": len(req.channels), "version": version}
