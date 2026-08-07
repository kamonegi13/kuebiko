"""死活監視サービス (Phase 1.5)。

Ollama / Discord webhook / IMAP の疎通を確認する。
UI の死活タブから利用される。各チェックは独立で、失敗してもブロックしない。
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from src.config_loader import AppConfig
from src.logging_config import get_logger

_log = get_logger(__name__)

CheckStatus = Literal["ok", "warning", "error"]


class HealthCheck(BaseModel):
    """単一の死活チェック結果。"""

    model_config = ConfigDict(frozen=True)

    name: str
    status: CheckStatus
    detail: str = ""


async def check_ollama(base_url: str) -> HealthCheck:
    """Ollama サーバ全体の疎通 (/api/tags) を確認する。"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
        if resp.status_code == 200:
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            return HealthCheck(
                name="ollama",
                status="ok",
                detail=f"models: {len(models)}",
            )
        return HealthCheck(
            name="ollama",
            status="error",
            detail=f"HTTP {resp.status_code}",
        )
    except Exception as e:  # noqa: BLE001
        return HealthCheck(name="ollama", status="error", detail=str(e))


async def list_ollama_models(base_url: str) -> list[str]:
    """Ollama `/api/tags` から pull 済モデル名を列挙する (モデルティア dropdown 用)。

    疎通失敗時は空 list (UI は空でも壊れない)。whitelist フィルタは呼び出し側で行う。
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
    resp.raise_for_status()
    data = resp.json()
    names = [m.get("name", "") for m in data.get("models", [])]
    return [n for n in names if n]


async def check_ollama_model(
    base_url: str,
    model_name: str,
    *,
    role: str,
) -> HealthCheck:
    """Phase 4: 設定されたモデルが Ollama に pull 済かを確認する。

    ``role`` は表示用の役割名 (main / extract / embed)。
    モデル未設定なら warning、存在しないなら error、存在すれば ok。
    """
    if not model_name:
        return HealthCheck(
            name=f"ollama_{role}",
            status="warning",
            detail="モデル未設定",
        )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
        if resp.status_code != 200:
            return HealthCheck(
                name=f"ollama_{role}",
                status="error",
                detail=f"/api/tags HTTP {resp.status_code}",
            )
        data = resp.json()
        # ollama list は "name" フィールドが "gemma4:31b" 形式 (or "name:latest")
        # 設定が "gemma4:26b" でも "gemma4:26b:latest" がヒットすることがあるので
        # ":" で前方一致もサポート
        existing = [m.get("name", "") for m in data.get("models", [])]
        if model_name in existing:
            return HealthCheck(
                name=f"ollama_{role}",
                status="ok",
                detail=f"{model_name} (pulled)",
            )
        # ":latest" 補完での一致もチェック
        if f"{model_name}:latest" in existing or any(
            m.startswith(model_name + ":") for m in existing
        ):
            return HealthCheck(
                name=f"ollama_{role}",
                status="ok",
                detail=f"{model_name} (pulled, tag normalized)",
            )
        return HealthCheck(
            name=f"ollama_{role}",
            status="error",
            detail=f"{model_name} 未 pull (`ollama pull {model_name}` で取得)",
        )
    except Exception as e:  # noqa: BLE001
        return HealthCheck(name=f"ollama_{role}", status="error", detail=str(e))


async def check_imap(
    host: str,
    port: int,
    user: str,
    password: str,
) -> HealthCheck:
    """Phase 4: IMAP 接続 + LOGIN が成功するか確認する (Grok 経路の前提条件)。"""
    if not (host and user and password):
        return HealthCheck(
            name="imap",
            status="warning",
            detail="IMAP 認証情報未設定 (Grok 経路は無効)",
        )

    def _connect_and_login() -> str:
        # imaplib は同期 API なので thread で実行する
        import imaplib

        with imaplib.IMAP4_SSL(host, port, timeout=10) as conn:
            conn.login(user, password)
            conn.logout()
        return "LOGIN OK"

    try:
        detail = await asyncio.to_thread(_connect_and_login)
        return HealthCheck(name="imap", status="ok", detail=f"{host}:{port} {detail}")
    except Exception as e:  # noqa: BLE001
        return HealthCheck(name="imap", status="error", detail=str(e))


async def check_discord_webhook(name: str, url: str) -> HealthCheck:
    """webhook URL が valid か (HEAD /webhooks/{id} で確認)。"""
    if not url:
        return HealthCheck(name=name, status="warning", detail="URL 未設定")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            return HealthCheck(name=name, status="ok", detail="webhook reachable")
        return HealthCheck(name=name, status="error", detail=f"HTTP {resp.status_code}")
    except Exception as e:  # noqa: BLE001
        return HealthCheck(name=name, status="error", detail=str(e))


async def check_tier_model(config: AppConfig, tier: Any, role: str) -> HealthCheck:
    """ティア割当モデルの死活を **提供元に応じて** 確認する (2026-07-20)。

    旧実装は常に Ollama の pull 済確認をしていたため、外部割当 (claudecode:/anthropic:)
    を「Ollama に無いモデル」= ERROR と誤判定していた。外部の到達不可はローカル既定への
    自動フォールバックで運用が継続するため、error でなく **warning** で報告する
    (error はフォールバックの無い真の障害 = ローカルモデル未 pull 等に限定)。
    """
    from src.tools.model_tiers import (
        ANTHROPIC_MODEL_PREFIX,
        CLAUDECODE_MODEL_PREFIX,
        resolve_tier_model,
    )

    ref = resolve_tier_model(tier)
    if ref.startswith(CLAUDECODE_MODEL_PREFIX):
        return await _check_claude_code_bridge(config, ref, role)
    if ref.startswith(ANTHROPIC_MODEL_PREFIX):
        return await _check_anthropic_api(config, ref, role)
    return await check_ollama_model(config.ollama_base_url, ref, role=role)


async def _check_claude_code_bridge(config: AppConfig, ref: str, role: str) -> HealthCheck:
    name = f"llm_{role}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{config.claude_code_bridge_url.rstrip('/')}/health")
        payload = resp.json() if resp.status_code == 200 else {}
        if payload.get("ok"):
            return HealthCheck(name=name, status="ok", detail=f"{ref} (外部LLM接続 稼働)")
        detail = str(payload.get("detail") or f"HTTP {resp.status_code}")[:120]
        return HealthCheck(
            name=name,
            status="warning",
            detail=f"{ref}: {detail} — ローカル既定へ自動切替中",
        )
    except Exception as e:  # noqa: BLE001
        return HealthCheck(
            name=name,
            status="warning",
            detail=f"{ref}: 外部LLM接続停止 ({type(e).__name__}) — ローカル既定へ自動切替中",
        )


async def _check_anthropic_api(config: AppConfig, ref: str, role: str) -> HealthCheck:
    name = f"llm_{role}"
    if not config.anthropic_api_key.strip():
        return HealthCheck(
            name=name,
            status="warning",
            detail=f"{ref}: ANTHROPIC_API_KEY 未設定 — ローカル既定へ自動切替中",
        )
    try:
        # /v1/models は無償のメタデータ endpoint — キー有効性と到達性を検証できる
        # (クレジット残高は呼出時にのみ判明する点は detail に明記)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": config.anthropic_api_key.strip(),
                    "anthropic-version": "2023-06-01",
                },
            )
        if resp.status_code == 200:
            return HealthCheck(
                name=name, status="ok", detail=f"{ref} (API 認証OK — 残高は呼出時判明)"
            )
        if resp.status_code in (401, 403):
            return HealthCheck(
                name=name,
                status="warning",
                detail=f"{ref}: API キー認証エラー — ローカル既定へ自動切替中",
            )
        return HealthCheck(
            name=name,
            status="warning",
            detail=f"{ref}: API HTTP {resp.status_code} — ローカル既定へ自動切替中",
        )
    except Exception as e:  # noqa: BLE001
        return HealthCheck(
            name=name,
            status="warning",
            detail=f"{ref}: API 到達不可 ({type(e).__name__}) — ローカル既定へ自動切替中",
        )


async def run_all_checks(config: AppConfig) -> list[HealthCheck]:
    """すべての死活チェックを並列実行する。"""
    from src.tools.model_tiers import Tier

    # 各能力ティアに割り当てられた実モデルの死活 (提供元対応: Ollama/bridge/Anthropic API)。
    tasks: list[Any] = [
        check_ollama(config.ollama_base_url),
        check_tier_model(config, Tier.REASONING, "reasoning"),
        check_tier_model(config, Tier.FAST, "fast"),
        check_tier_model(config, Tier.DIALOG, "dialog"),
        check_tier_model(config, Tier.EMBEDDING, "embedding"),
        check_imap(
            config.imap_host,
            config.imap_port,
            config.imap_user,
            config.imap_password,
        ),
    ]
    # C1: チャンネルレジストリ駆動 (custom channel の webhook も死活対象に含める)
    from src.tools.channel_registry import resolve_webhooks

    for ch_name, url in resolve_webhooks(config.discord_webhooks).items():
        tasks.append(check_discord_webhook(f"discord_{ch_name}", url))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[HealthCheck] = []
    for r in results:
        if isinstance(r, BaseException):
            out.append(HealthCheck(name="?", status="error", detail=str(r)))
        else:
            out.append(r)

    # ホスト復旧 watchdog は**異常のときだけ**出す (2026-08-02)。常設の疎通チェックと違い、
    # 正常時は黙っているのが正しい — 常時 1 行増やすと本当に見るべき異常が埋もれる。
    wd = _host_watchdog_check()
    if wd is not None:
        out.append(wd)

    return out


def _host_watchdog_check() -> HealthCheck | None:
    """ホスト復旧 watchdog の異常のみを死活項目として返す (正常/未導入は None)。

    「復旧を実行した」も出す — 自動で直ったこと自体が、スリープ復帰の失敗が
    起きているという運用上のシグナルだから (黙って直すと傾向が見えない)。
    """
    from src.tools import host_watchdog_files as hwf

    state = hwf.read_state()
    if not state or not hwf.is_enabled():
        return None  # 未導入 / 無効は「異常」ではない
    status = str(state.get("status", ""))
    if status == "degraded":
        fails = state.get("consecutive_failures", 0)
        return HealthCheck(
            name="host_watchdog",
            status="warning",
            detail=f"Docker が無応答 ({fails} 回連続)。閾値到達で自動復旧します",
        )
    if status == "recovery_failed":
        return HealthCheck(
            name="host_watchdog",
            status="error",
            detail=str(state.get("detail") or "自動復旧に失敗しました (手動確認が必要)"),
        )
    if status == "recovered":
        return HealthCheck(
            name="host_watchdog",
            status="warning",
            detail=f"Docker 無応答から自動復旧しました ({state.get('last_recovery_at', '')})",
        )
    return None
