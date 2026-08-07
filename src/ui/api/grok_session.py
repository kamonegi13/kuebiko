"""Grok セッション管理 API (2026-07-05)。

Grok アカウント変更・認証失効時の再取得を UI から一気通貫にする:
**状態可視化 → 再取得ガイド (host コマンド 1 つ) → 完了自動検知 (mtime) → 検証**。

cookie 抽出そのもの (``scripts/grok_extract_cookies.py``) は Chrome の暗号化 cookie を
macOS Keychain で復号するため**ホストでしか実行できない** — コンテナ側 (本 API) は
状態表示と production 同条件のセッション検証を担い、ユーザー操作を
「ターミナルでコマンド 1 回」に圧縮する。

セキュリティ (§12):
- cookie の**値も名前も一切返さない** (件数とドメイン別集計のみ)
- POST (verify) は READ_ONLY middleware が 403 で block (個別ガード不要)
- path は定数 (ユーザー入力の path 解決なし)
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from src.logging_config import get_logger

grok_session_api = APIRouter(prefix="/api/v1/grok/session", tags=["grok"])

_log = get_logger(__name__)

_STATE_PATH = Path("data/playwright/state.json")
_VERIFY_STATUS_PATH = Path("data/playwright/verify_status.json")
_VERIFY_TIMEOUT_SECONDS = 90.0
# 再取得ガイドに表示するホスト側コマンド (grok_extract_cookies.py の docstring が正)
ACQUIRE_COMMAND = "uv run python scripts/grok_extract_cookies.py"


def _state_summary() -> dict[str, Any]:
    """state.json の概況 (cookie の値・名前は含めない)。"""
    if not _STATE_PATH.exists():
        return {"exists": False}
    stat = _STATE_PATH.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    age_hours = (datetime.now(UTC) - modified).total_seconds() / 3600
    cookie_count = 0
    domains: dict[str, int] = {}
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        for cookie in data.get("cookies", []):
            cookie_count += 1
            domain = str(cookie.get("domain", "")).lstrip(".")
            if domain:
                domains[domain] = domains.get(domain, 0) + 1
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("grok_state_parse_failed", error=str(e))
    return {
        "exists": True,
        "modified_at": modified.isoformat(),
        "age_hours": round(age_hours, 1),
        "cookie_count": cookie_count,
        "domains": dict(sorted(domains.items())),
    }


def _translate_failure_reason(reason: str | None) -> str:
    """GrokFetcher の failure_reason (http_error_403 等の生値) を日本語文に写像する。"""
    if not reason:
        return "不明"
    if reason.startswith("timeout"):
        return "タイムアウトしました"
    if reason.startswith("http_error_"):
        return f"HTTP エラー ({reason.removeprefix('http_error_')})"
    if reason == "session_expired":
        return "セッションが失効しています"
    return "不明"


def _read_last_verify() -> dict[str, Any] | None:
    if not _VERIFY_STATUS_PATH.exists():
        return None
    try:
        data = json.loads(_VERIFY_STATUS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _last_grok_run() -> dict[str, Any] | None:
    """直近の grok-briefing run の成否 (セッション失効の手掛かり)。"""
    try:
        from src.storage.run_history import RunHistoryRepository

        repo = RunHistoryRepository()
        for run in repo.list_runs(limit=200):
            if run.pipeline != "grok-briefing":
                continue
            note = run.note or ""
            return {
                "status": run.status,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "total_fetched": run.total_fetched,
                "session_expired": ("セッション失効" in note) or ("session_expired" in note),
            }
    except Exception as e:  # noqa: BLE001 — 表示補助の失敗で status 自体は返す
        _log.warning("grok_last_run_lookup_failed", error=str(e))
    return None


@grok_session_api.get("")
async def get_session_status() -> dict[str, Any]:
    """Grok セッションの現況 (state.json 概況 + 最終検証 + 直近 run)。"""
    return {
        "state": _state_summary(),
        "last_verify": _read_last_verify(),
        "last_run": _last_grok_run(),
        "acquire_command": ACQUIRE_COMMAND,
    }


@grok_session_api.post("/verify")
async def verify_session() -> dict[str, Any]:
    """production fetcher と同条件 (headless + state.json) でセッションを実地検証する。

    判定は pipeline 本体と同じ規約: login redirect / 401 → session_expired。
    結果は sidecar (verify_status.json) に永続化し、GET が返す。
    """
    from src.grok.fetcher import GrokFetcher

    checked_at = datetime.now(UTC).isoformat()
    status = "error"
    note = ""
    final_url = ""
    if not _STATE_PATH.exists():
        status = "no_state"
        note = "認証情報が保存されていません (再取得が必要です)"
    else:
        try:
            async with GrokFetcher() as fetcher:
                result = await asyncio.wait_for(
                    fetcher.fetch("https://grok.com"),
                    timeout=_VERIFY_TIMEOUT_SECONDS,
                )
            final_url = result.final_url or ""
            if result.success:
                status = "ok"
            elif result.failure_reason == "session_expired":
                status = "session_expired"
            else:
                note = _translate_failure_reason(result.failure_reason)
        except TimeoutError:
            note = f"検証タイムアウト ({_VERIFY_TIMEOUT_SECONDS:.0f}s)"
        except Exception as e:  # noqa: BLE001 — 検証失敗は結果として返す (500 にしない)
            _log.warning("grok_session_verify_failed", error=f"{type(e).__name__}: {e}")
            note = "検証中にエラーが発生しました"

    payload = {
        "status": status,
        "checked_at": checked_at,
        "final_url": final_url,
        "note": note,
    }
    try:
        _VERIFY_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _VERIFY_STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        _log.warning("grok_verify_status_write_failed", error=str(e))
    _log.info("grok_session_verified", status=status, note=note)
    return payload
