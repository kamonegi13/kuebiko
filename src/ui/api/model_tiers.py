"""LLM モデルティア管理 API (能力ティア → 実モデルの割当)。

GET  /api/v1/model-tiers   現在のティア割当 + 選択可能モデル (中華系除外) + ティア説明
POST /api/v1/model-tiers   検証して DB (config_store) に版保存 + キャッシュ無効化

本質: ツールが「step → 能力ティア」を決め (model_tiers.STEP_REGISTRY, code 所有)、
ユーザはこの画面で「ティア → 実モデル」だけを決める。per-step のモデル割当は UI に出さない
(粒度過多・誤設定の温床)。dropdown は Ollama `/api/tags` から動的取得し、中華系は
whitelist で除外する (§4 の3層防御の上流)。保存時も ``validate_model_tiers`` が中華系を弾く。
write は READ_ONLY middleware (app.py) が 403 で block するため個別ガード不要。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger

model_tiers_api = APIRouter(prefix="/api/v1/model-tiers", tags=["model-tiers"])

_log = get_logger(__name__)

# ティア → UI 表示メタ (label + 説明 + 担う処理例)。ティア集合は code 所有。
TIER_META: dict[str, dict[str, str]] = {
    "reasoning": {
        "label": "構造化分析 (reasoning)",
        "description": (
            "台帳の構造化分析 (昼の即時判定)。ACH 仮説採点・敵対的検証・増分評価・"
            "報告書射影。拡張思考は常に無効 (実行時間の制約)。"
        ),
        "examples": "台帳 ACH / adversarial / 増分評価 / 報告書射影",
    },
    "narrative": {
        "label": "散文生成・夜間精査 (narrative)",
        "description": (
            "読み物の散文生成と、台帳 ACH の夜間精査 (当日判定の think 再評価)。"
            "外部モデル割当時は拡張思考 (think) を使えます (下の設定)。"
            "夜間精査は拡張思考が有効な構成のときだけ実行されます。"
        ),
        "examples": "状況総括の文章 / PIR Spotlight / 夜間精査",
    },
    "fast": {
        "label": "軽量 (fast)",
        "description": (
            "高スループットの収集系バッチ。記事要約・翻訳・triage・抽出・deep-dive・PIR要点。"
            "呼出 ~2500 回/日 — 外部モデル割当はコスト大 (月2億トークン級) なので注意。"
        ),
        "examples": "記事要約 / 本文翻訳 / detect-new / 抽出 / PIR focus",
    },
    "dialog": {
        "label": "対話 (dialog)",
        "description": (
            "user-facing の対話系。低頻度なので外部モデルを割り当てても収集系のコストに波及しない。"
        ),
        "examples": "分析チャット / LLM支援検索 / PIR compile / selector 提案",
    },
    "embedding": {
        "label": "埋込 (embedding)",
        "description": "意味的重複排除・検索の埋込モデル。空にすると意味的 dedup が無効化。",
        "examples": "semantic dedup / 検索",
    },
}


class SaveModelTiersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiers: dict[str, str]
    # narrative ティアの拡張思考: "auto" (外部モデル割当時のみ ON) / "off"。None = 変更なし
    narrative_think: str | None = None


# Models API の取得キャッシュ (TTL 1h)。ハードコード一覧は将来モデルに追随しないため、
# API キー設定時は動的取得し、失敗時のみ curated 定数へ fail-soft する。
_MODELS_CACHE: dict[str, tuple[float, list[str]]] = {}
_MODELS_CACHE_TTL = 3600.0


async def _fetch_endpoint_models(name: str, base_url: str, api_key: str) -> list[str]:
    """接続先の /models からモデル一覧 (TTL キャッシュ・失敗は空 = 選択肢なしで表示継続)。"""
    import time

    from src.tools.llm_client import is_model_allowed
    from src.tools.openai_compat_client import list_openai_compat_model_ids

    cache_key = f"ep:{name}"
    cached = _MODELS_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < _MODELS_CACHE_TTL:
        return cached[1]
    try:
        fetched = await list_openai_compat_model_ids(base_url, api_key)
        ids = [m for m in fetched if is_model_allowed(m)]
    except Exception as e:  # noqa: BLE001 — 接続先が落ちていても画面は出す
        _log.warning("endpoint_models_fetch_failed", endpoint=name, error=str(e)[:150])
        return []
    _MODELS_CACHE[cache_key] = (time.monotonic(), ids)
    return ids


async def _fetch_anthropic_model_ids(api_key: str) -> list[str] | None:
    """Models API からモデル id を取得 (TTL キャッシュ・失敗は None = fallback 合図)。"""
    import time

    from src.tools.anthropic_client import list_anthropic_model_ids

    cached = _MODELS_CACHE.get("ids")
    if cached and time.monotonic() - cached[0] < _MODELS_CACHE_TTL:
        return cached[1]
    try:
        ids = await list_anthropic_model_ids(api_key)
    except Exception as e:  # noqa: BLE001 — 取得失敗は curated fallback (画面は壊さない)
        _log.warning("anthropic_models_fetch_failed", error=str(e)[:200])
        return None
    _MODELS_CACHE["ids"] = (time.monotonic(), ids)
    return ids


def _model_family(model_id: str) -> str:
    """モデル id → 系列名 ("claude-fable-5" → "fable", "claude-opus-4-8" → "opus")。"""
    return model_id.removeprefix("claude-").split("-")[0]


def _latest_per_family(model_ids: list[str]) -> list[str]:
    """系列ごとの最新 1 件に絞る (Models API は新しい順で返す)。

    旧世代の完全名を全部並べると dropdown が雑然とする (利用者指摘 2026-07-24)。
    pin 需要が出たら見直す — 現状はエイリアス (最新自動解決) + 系列最新で足りる。
    """
    seen: set[str] = set()
    out: list[str] = []
    for mid in model_ids:
        fam = _model_family(mid)
        if fam in seen:
            continue
        seen.add(fam)
        out.append(mid)
    return out


def build_external_choices(
    fetched_ids: list[str] | None,
    *,
    key_enabled: bool,
    bridge_enabled: bool,
) -> tuple[list[str], list[str]]:
    """(anthropic 選択肢, claudecode 選択肢) を組み立てる純関数。

    - anthropic: 取得成功時は**系列ごとの最新のみ** (fable/sonnet/opus/haiku 各 1)、
      失敗時は curated 定数 (キー無しは空)。
    - claudecode: エイリアス (sonnet 等 — CLI が最新版へ自動解決) を先頭に、
      **エイリアスでカバーされない系列の最新のみ**完全名で追加 (例: fable)。
      bridge 疎通時のみ。
    - どちらも中華系 denylist を通す (3 層防御の dropdown 層)。
    """
    from src.tools.llm_client import is_model_allowed
    from src.tools.model_tiers import (
        ANTHROPIC_MODEL_CHOICES,
        ANTHROPIC_MODEL_PREFIX,
        CLAUDE_CODE_MODEL_CHOICES,
        CLAUDECODE_MODEL_PREFIX,
    )

    latest = _latest_per_family([m for m in (fetched_ids or []) if is_model_allowed(m)])
    anthropic: list[str] = []
    if key_enabled:
        anthropic = (
            [f"{ANTHROPIC_MODEL_PREFIX}{m}" for m in latest]
            if latest
            else list(ANTHROPIC_MODEL_CHOICES)
        )
    claude_code: list[str] = []
    if bridge_enabled:
        claude_code = list(CLAUDE_CODE_MODEL_CHOICES)
        alias_families = {c.removeprefix(CLAUDECODE_MODEL_PREFIX) for c in claude_code}
        claude_code += [
            f"{CLAUDECODE_MODEL_PREFIX}{m}"
            for m in latest
            if _model_family(m) not in alias_families
        ]
    return anthropic, claude_code


@model_tiers_api.get("")
async def get_model_tiers() -> dict[str, Any]:
    """現在のティア割当 + 選択可能モデル (中華系除外) + ティア説明。"""
    from src.config_loader import load_app_config
    from src.tools.llm_client import is_model_allowed
    from src.tools.model_tiers import (
        BUILTIN_MODEL_TIERS,
        invalidate_model_tiers_cache,
        load_model_tiers,
        resolve_narrative_think,
    )
    from src.ui.services.health import list_ollama_models

    cfg = load_app_config()
    invalidate_model_tiers_cache()
    tiers = load_model_tiers()
    narrative_think = resolve_narrative_think()

    available: list[str] = []
    excluded: list[str] = []
    ollama_error: str | None = None
    try:
        for name in await list_ollama_models(cfg.ollama_base_url):
            (available if is_model_allowed(name) else excluded).append(name)
    except Exception as e:  # noqa: BLE001 — Ollama 不通でも画面は現割当を表示できる
        ollama_error = str(e)
        _log.warning("model_tiers_list_ollama_failed", error=ollama_error)

    # 外部 LLM (§4 改訂): ANTHROPIC_API_KEY 設定時のみ選択肢に出す (未設定 = 従来のローカルのみ)。
    from src.ui.services.env_editor import mask_value

    external_enabled = bool(cfg.anthropic_api_key.strip())
    # Claude Code サブスク bridge: 疎通しているときだけ選択肢に出す (未起動 = 非表示)。
    # 消費表示は 2026-07-26 に DB (llm_usage) へ一本化 — bridge health からは
    # auth/update のみ読む (bridge 側 JSONL 自己観測は内部 debug 用として残置)。
    claude_code_enabled = False
    claude_code_auth: dict[str, Any] | None = None
    claude_code_update: dict[str, Any] | None = None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=0.8) as hc:
            r = await hc.get(f"{cfg.claude_code_bridge_url.rstrip('/')}/health")
            payload = r.json() if r.status_code == 200 else {}
            claude_code_enabled = bool(payload.get("ok"))
            auth = payload.get("auth")
            claude_code_auth = auth if isinstance(auth, dict) else None
            upd = payload.get("update")
            claude_code_update = upd if isinstance(upd, dict) else None
    except Exception:  # noqa: BLE001 — bridge 未起動は正常系 (選択肢を出さないだけ)
        claude_code_enabled = False
    # 外部モデル選択肢: API キー設定時は Models API から動的取得 (将来モデルへ自動追随)。
    fetched_ids = (
        await _fetch_anthropic_model_ids(cfg.anthropic_api_key) if external_enabled else None
    )
    external_choices, claude_code_choices = build_external_choices(
        fetched_ids, key_enabled=external_enabled, bridge_enabled=claude_code_enabled
    )
    # ユーザ定義接続先 (OpenAI 互換): 登録済みのものだけを返す (未登録ベンダーは並ばない)。
    from src.storage.run_history import RunHistoryRepository
    from src.tools.llm_endpoints import (
        ENDPOINT_PRESETS,
        endpoint_api_key,
        load_llm_endpoints,
    )
    from src.ui.services.env_editor import mask_value as _mask

    try:
        usage_by_provider = RunHistoryRepository().llm_usage_summary()
    except Exception as e:  # noqa: BLE001 — 消費集計の失敗で画面を壊さない
        _log.warning("llm_usage_summary_failed", error=str(e)[:150])
        usage_by_provider = {}
    endpoints_payload = []
    for ep in load_llm_endpoints():
        key = endpoint_api_key(ep.name)
        models = await _fetch_endpoint_models(ep.name, ep.base_url, key)
        endpoints_payload.append(
            {
                "name": ep.name,
                "base_url": ep.base_url,
                "key_env": ep.key_env,
                "key_set": bool(key),
                "key_masked": _mask(key) if key else "",
                "models": [f"{ep.name}:{m}" for m in models],
                "usage": usage_by_provider.get(ep.name),
            }
        )
    return {
        "tiers": tiers,
        "narrative_think": narrative_think,
        "meta": TIER_META,
        # コード既定 (fresh DB / rollback の bootstrap 値)。UI の「既定に戻す」参照用。
        "builtin": dict(BUILTIN_MODEL_TIERS),
        "available": sorted(available),
        "external": external_choices,
        "external_enabled": external_enabled,
        "claude_code": claude_code_choices,
        "claude_code_enabled": claude_code_enabled,
        # 消費台帳の一本化 (2026-07-26): DB llm_usage から。形は旧 bridge payload 互換
        # (window_5h/today/days7 + last_call_at) なので frontend 変更なし。
        "claude_code_usage": usage_by_provider.get("claudecode"),
        "claude_code_auth": claude_code_auth,
        "claude_code_update": claude_code_update,
        "claude_code_bridge_url": cfg.claude_code_bridge_url,
        # ユーザ定義接続先 (登録済みのみ) + 追加ダイアログ用プリセット + 消費集計
        "endpoints": endpoints_payload,
        "endpoint_presets": list(ENDPOINT_PRESETS),
        "anthropic_usage": usage_by_provider.get("anthropic"),
        # 組み込みローカルエンジン (接続先パネルの常設行。全ティアの fallback 先のため削除不可)
        "ollama_base_url": cfg.ollama_base_url,
        # 平文キーは返さない (§4/§12 — readonly instance でも安全なマスク表示のみ)
        "anthropic_key_masked": mask_value(cfg.anthropic_api_key.strip()),
        # 中華系ゆえ選択肢から除外したモデル (透明性のため件数/名前を返す)。
        "excluded": sorted(excluded),
        "ollama_error": ollama_error,
    }


class SaveAnthropicKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str


@model_tiers_api.post("/anthropic-key")
async def save_anthropic_key(request: Request, req: SaveAnthropicKeyRequest) -> dict[str, Any]:
    """外部 LLM (Anthropic) の API キーを .env に保存/削除する (UI 完結・即時反映)。

    保存先は DB でなく **.env** — config_store は版履歴 (app_config_versions) と日次
    pg_dump backup に値が残り続けるため秘密情報を置かない (§4「認証情報は .env」)。
    AppConfig は request ごとに .env を再読込するため保存は即時反映 (再起動不要)。
    空文字 = 削除 (外部 LLM 無効化)。ただし anthropic:* を割当中のティアが残っている
    場合は 400 で拒否する (06:30 の run で初めて壊れる事故を設定時に防ぐ)。
    """
    from src.tools.model_tiers import (
        ANTHROPIC_MODEL_PREFIX,
        invalidate_model_tiers_cache,
        load_model_tiers,
    )
    from src.ui.services.env_editor import EnvEditError, mask_value, update_env

    api_key = req.api_key.strip()
    if not api_key:
        invalidate_model_tiers_cache()
        assigned = [
            tier
            for tier, model in load_model_tiers().items()
            if model.startswith(ANTHROPIC_MODEL_PREFIX)
        ]
        if assigned:
            labels = [TIER_META.get(t, {}).get("label", t) for t in sorted(assigned)]
            raise HTTPException(
                status_code=400,
                detail=(
                    f"ティア「{'、'.join(labels)}」が外部モデル割当のままです。"
                    "先にローカルモデルへ戻してからキーを削除してください"
                ),
            )

    editor = request.app.state.file_editor
    try:
        update_env(editor.env_path(), {"ANTHROPIC_API_KEY": api_key})
    except EnvEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _log.info("anthropic_key_saved", enabled=bool(api_key))
    return {
        "saved": True,
        "external_enabled": bool(api_key),
        "anthropic_key_masked": mask_value(api_key),
    }


class SaveOllamaUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str


@model_tiers_api.post("/ollama-url")
async def save_ollama_url(request: Request, req: SaveOllamaUrlRequest) -> dict[str, Any]:
    """Ollama 接続 URL を .env に保存する (設定・死活の画面統合 P3、即時反映)。

    compose が OLLAMA_BASE_URL を process env に注入するため (.env 補間、コンテナ作成時
    固定)、.env 書込だけでは pydantic-settings の environ 優先で陰に隠れる。os.environ
    にも同期して即時反映し、コンテナ再作成時は compose の ${OLLAMA_BASE_URL:-…} 補間が
    .env から同じ値を拾う (書込元をこの API に一本化して整合を保つ)。
    保存直後に /api/tags で疎通を確認し、結果を同梱して返す (UI が即フィードバック)。
    """
    import os

    from src.ui.services.env_editor import EnvEditError, update_env
    from src.ui.services.health import list_ollama_models

    base_url = req.base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="http:// または https:// で始まる URL を指定してください",
        )

    editor = request.app.state.file_editor
    try:
        update_env(editor.env_path(), {"OLLAMA_BASE_URL": base_url})
    except EnvEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    os.environ["OLLAMA_BASE_URL"] = base_url

    model_count: int | None = None
    error: str | None = None
    try:
        model_count = len(await list_ollama_models(base_url))
    except Exception as e:  # noqa: BLE001 — 疎通失敗でも保存自体は成立している
        error = str(e)
    _log.info("ollama_url_saved", reachable=error is None)
    return {
        "saved": True,
        "ollama_base_url": base_url,
        "model_count": model_count,
        "error": error,
    }


@model_tiers_api.post("/claudecode-update")
async def claudecode_update() -> dict[str, Any]:
    """bridge に CLI 自己更新を指示する (UI の「更新」ボタン)。"""
    import httpx

    from src.config_loader import load_app_config

    cfg = load_app_config()
    try:
        async with httpx.AsyncClient(timeout=200) as hc:
            r = await hc.post(f"{cfg.claude_code_bridge_url.rstrip('/')}/v1/update")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"bridge に接続できません: {e}") from e
    if r.status_code != 200:
        detail = ""
        try:
            detail = str(r.json().get("detail") or "")[:300]
        except Exception:  # noqa: BLE001
            detail = r.text[:300]
        raise HTTPException(status_code=502, detail=detail or f"HTTP {r.status_code}")
    result: dict[str, Any] = r.json()
    _log.info("claudecode_cli_updated", **{k: result.get(k) for k in ("updated", "after")})
    return result


class SaveClaudeCodeTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str


@model_tiers_api.post("/claudecode-token")
async def save_claudecode_token(
    request: Request, req: SaveClaudeCodeTokenRequest
) -> dict[str, Any]:
    """Claude Code サブスクの長期トークンを .env に保存/削除する (即時反映)。

    sidecar bridge は呼出ごとに共有 .env を読むため、保存は bridge 再起動なしで効く。
    空文字 = 削除 (bridge は CLI 既定認証 = 旧ホスト方式のログイン状態に fallback)。
    """
    from src.ui.services.env_editor import EnvEditError, mask_value, update_env

    token = req.token.strip()
    editor = request.app.state.file_editor
    try:
        update_env(editor.env_path(), {"CLAUDE_CODE_OAUTH_TOKEN": token})
    except EnvEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _log.info("claudecode_token_saved", enabled=bool(token))
    return {
        "saved": True,
        "token_set": bool(token),
        "token_masked": mask_value(token) if token else "",
    }


class SaveEndpointsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoints: list[dict[str, str]]


@model_tiers_api.post("/endpoints")
async def save_llm_endpoints_api(req: SaveEndpointsRequest) -> dict[str, Any]:
    """接続先一覧を検証して DB (config_store) に版保存する。

    削除ガード: いずれかのティアに割当中の接続先は削除できない (400)。
    """
    from src.storage.config_store import save_config
    from src.tools.llm_endpoints import (
        LLM_ENDPOINTS_CONFIG_KEY,
        invalidate_llm_endpoints_cache,
        load_llm_endpoints,
        validate_llm_endpoints,
    )
    from src.tools.model_tiers import invalidate_model_tiers_cache, load_model_tiers

    doc = {"endpoints": req.endpoints}
    errs = validate_llm_endpoints(doc)
    if errs:
        raise HTTPException(status_code=400, detail="; ".join(errs))

    new_names = {str(e.get("name")) for e in req.endpoints}
    invalidate_llm_endpoints_cache()
    removed = {e.name for e in load_llm_endpoints()} - new_names
    if removed:
        invalidate_model_tiers_cache()
        in_use = {
            m.split(":", 1)[0]
            for m in load_model_tiers().values()
            if ":" in m and m.split(":", 1)[0] in removed
        }
        if in_use:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"接続先「{'、'.join(sorted(in_use))}」はティアに割当中のため削除できません。"
                    "先にモデル割当を変更してください"
                ),
            )

    version = save_config(LLM_ENDPOINTS_CONFIG_KEY, doc, note="UI 保存 (LLM 接続先)")
    invalidate_llm_endpoints_cache()
    _MODELS_CACHE.clear()
    _log.info("llm_endpoints_saved", count=len(req.endpoints), version=version)
    return {"saved": True, "count": len(req.endpoints), "version": version}


class SaveEndpointKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    api_key: str


@model_tiers_api.post("/endpoint-key")
async def save_endpoint_key(request: Request, req: SaveEndpointKeyRequest) -> dict[str, Any]:
    """接続先の API キーを .env に保存/削除する (登録済み接続先のみ、即時反映)。"""
    from src.tools.llm_endpoints import invalidate_llm_endpoints_cache, resolve_endpoint
    from src.ui.services.env_editor import EnvEditError, mask_value, update_env

    invalidate_llm_endpoints_cache()
    ep = resolve_endpoint(req.name)
    if ep is None:
        raise HTTPException(status_code=404, detail="未登録の接続先です")
    editor = request.app.state.file_editor
    try:
        update_env(editor.env_path(), {ep.key_env: req.api_key.strip()})
    except EnvEditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _MODELS_CACHE.pop(f"ep:{req.name}", None)
    _log.info("llm_endpoint_key_saved", endpoint=req.name, enabled=bool(req.api_key.strip()))
    return {
        "saved": True,
        "key_set": bool(req.api_key.strip()),
        "key_masked": mask_value(req.api_key.strip()) if req.api_key.strip() else "",
    }


@model_tiers_api.post("")
async def save_model_tiers(req: SaveModelTiersRequest) -> dict[str, Any]:
    """ティア割当を検証 (中華系拒否) して保存 (版履歴は config_store に残る)。"""
    from src.storage.config_store import save_config
    from src.tools.model_tiers import (
        MODEL_TIERS_CONFIG_KEY,
        NARRATIVE_THINK_KEY,
        invalidate_model_tiers_cache,
        resolve_narrative_think,
        validate_model_tiers,
    )

    # narrative_think は同一 config doc の予約 key として保存 (未指定なら現値を維持)。
    invalidate_model_tiers_cache()
    think = req.narrative_think if req.narrative_think is not None else resolve_narrative_think()
    doc: dict[str, str] = {**req.tiers, NARRATIVE_THINK_KEY: think}
    errs = validate_model_tiers(doc)
    if errs:
        raise HTTPException(status_code=400, detail="; ".join(errs))

    version = save_config(MODEL_TIERS_CONFIG_KEY, doc, note="UI 保存 (モデルティア)")
    invalidate_model_tiers_cache()
    _log.info("model_tiers_saved", count=len(req.tiers), version=version)
    return {"saved": True, "count": len(req.tiers), "version": version}
