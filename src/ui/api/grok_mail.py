"""Grok メール受信 (IMAP) の設定 + 死活 API (設定・死活の画面統合 P2)。

.env の IMAP_* 4 キーをソース管理画面の「Grok メール受信」カードから一体管理する。
IMAP_HOST / IMAP_USER / IMAP_PASSWORD は secret 扱い (env_editor.BASE_SECRET_ENV_KEYS) —
レスポンスは常にマスク値のみ (公開 readonly instance への漏洩防止、security-review H2)。
write は READ_ONLY middleware (app.py) が 403 で block するため個別ガードは不要。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger

grok_mail_api = APIRouter(prefix="/api/v1/grok-mail", tags=["grok-mail"])

_log = get_logger(__name__)

_PORT_MIN = 1
_PORT_MAX = 65535


def _current_state() -> dict[str, Any]:
    """現在の IMAP 設定をマスク値で返す (平文は含めない)。"""
    from src.config_loader import load_app_config
    from src.ui.services.env_editor import mask_value

    cfg = load_app_config()
    return {
        "host_masked": mask_value(cfg.imap_host),
        "port": cfg.imap_port,
        "user_masked": mask_value(cfg.imap_user),
        "password_set": bool(cfg.imap_password),
        "configured": bool(cfg.imap_host and cfg.imap_user and cfg.imap_password),
    }


@grok_mail_api.get("")
async def get_grok_mail() -> dict[str, Any]:
    """Grok メール受信 (IMAP) の設定状況 (マスク表示のみ)。"""
    return _current_state()


@grok_mail_api.get("/health")
async def grok_mail_health() -> dict[str, Any]:
    """IMAP ログイン試行による死活確認 (旧 /health ページの check_imap を流用)。"""
    from src.config_loader import load_app_config
    from src.ui.services.health import check_imap

    cfg = load_app_config()
    check = await check_imap(cfg.imap_host, cfg.imap_port, cfg.imap_user, cfg.imap_password)
    return {"status": check.status, "detail": check.detail}


class SaveGrokMailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = ""
    port: str = ""
    user: str = ""
    password: str = ""


@grok_mail_api.post("")
async def save_grok_mail(request: Request, req: SaveGrokMailRequest) -> dict[str, Any]:
    """IMAP 設定を .env に保存する (即時反映・再起動不要)。

    空欄のフィールドは既存値を維持する (.env editor の secret 空送信 skip と同じ規約)。
    値の削除が必要なときはホストで .env を直接編集する (保存層として存続、docs/deployment.md)。
    """
    from src.ui.services.env_editor import EnvEditError, update_env

    updates: dict[str, str] = {}
    host = req.host.strip()
    if host:
        updates["IMAP_HOST"] = host
    port = req.port.strip()
    if port:
        if not port.isdigit() or not (_PORT_MIN <= int(port) <= _PORT_MAX):
            raise HTTPException(
                status_code=400,
                detail=f"ポート番号は {_PORT_MIN}〜{_PORT_MAX} の数値で指定してください",
            )
        updates["IMAP_PORT"] = port
    user = req.user.strip()
    if user:
        updates["IMAP_USER"] = user
    password = req.password.strip()
    if password:
        updates["IMAP_PASSWORD"] = password

    if not updates:
        raise HTTPException(status_code=400, detail="変更する項目がありません")

    editor = request.app.state.file_editor
    try:
        update_env(editor.env_path(), updates)
    except EnvEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _log.info("grok_mail_settings_saved", keys=sorted(updates.keys()))
    return {"saved": True, **_current_state()}
