"""運用 config の変更履歴 API (config_store の版履歴 + revert)。

GET  /api/v1/config-history                既知 config key の一覧 (現 version + 版数)
GET  /api/v1/config-history/{key}          1 key の版履歴 (新しい順、現行マーク付き)
GET  /api/v1/config-history/{key}/{version} 特定 version の値 (閲覧・diff 用)
POST /api/v1/config-history/{key}/revert   指定 version を新 version として復元 + cache 無効化

履歴 view は read-only。revert は write のため READ_ONLY middleware (app.py) が 403 で
block する (個別ガード不要)。key は whitelist 照合で任意 key へのアクセスを塞ぐ。
config の SSoT は DB (config_store)、これはその版履歴を可視化・巻き戻す管理画面。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger
from src.storage import config_store

config_history_api = APIRouter(prefix="/api/v1/config-history", tags=["config-history"])

_log = get_logger(__name__)

# 履歴を見せる運用 config の whitelist (key → 日本語ラベル)。
# config_store に載る他の内部 key を露出させない境界も兼ねる。
_KNOWN_KEYS: dict[str, str] = {
    "routing_rules": "ルーティングルール",
    "channels": "チャンネル",
    "product_routing": "プロダクト配信 (朝刊/夕刊・recap・synthesis・spotlight)",
    "source_quality": "ソース品質 (降格・cap)",
    "pir": "PIR (優先情報要件)",
    "match_lists": "マッチリスト (キーワード語彙)",
    "feeds": "ソース: RSS feed 購読",
    "watchers": "ソース: sitemap watcher",
    "scrapers": "ソース: html scraper",
    "model_tiers": "LLM モデル割当 (推論/軽量/埋込ティア)",
    "llm_endpoints": "LLM 接続先 (OpenAI 互換エンドポイント)",
    "jp_ci_operators": "重要インフラ 指定事業者名簿",
    "dashboard_layout": "ダッシュボード レイアウト (widget 配置 + 共有既定 config)",
    "grok_tasks": "Grok タスク定義 (外部 Grok 側設定の写し)",
}

# 一覧で版数を数える上限 (config 保存は低頻度なので十分大きい固定値)。
_HISTORY_LIMIT = 200


def _require_known_key(key: str) -> str:
    """whitelist 照合。label を返す。未知 key は 404。"""
    label = _KNOWN_KEYS.get(key)
    if label is None:
        raise HTTPException(status_code=404, detail="未知の設定項目です")
    return label


def _invalidate(key: str) -> None:
    """revert 後に該当 config のプロセスキャッシュを無効化し、次回 read に反映する。

    各 save API が呼ぶのと同じ無効化を踏襲する (import は遅延 = 循環回避)。
    """
    if key == "routing_rules":
        from src.cti.routing_rules import invalidate_rules_cache

        invalidate_rules_cache()
    elif key == "channels":
        from src.tools.channel_registry import invalidate_channels_cache

        invalidate_channels_cache()
    elif key == "product_routing":
        from src.tools.product_routing import invalidate_product_routing_cache

        invalidate_product_routing_cache()
    elif key == "source_quality":
        from src.cti.router import get_source_quality

        get_source_quality(force_reload=True)
    elif key == "pir":
        from src.pir.integration import invalidate_cache

        invalidate_cache()
    elif key == "match_lists":
        from src.cti.match_lists import invalidate_match_lists_cache

        invalidate_match_lists_cache()
    elif key in ("feeds", "watchers", "scrapers"):
        from src.sources.source_store import invalidate_caches

        transport = {"feeds": "rss", "watchers": "sitemap", "scrapers": "html_scraper"}[key]
        invalidate_caches(transport)  # type: ignore[arg-type]
    elif key == "jp_ci_operators":
        from src.cti.ci_operator_roster import invalidate_operators_cache

        invalidate_operators_cache()
    elif key == "model_tiers":
        from src.tools.model_tiers import invalidate_model_tiers_cache

        invalidate_model_tiers_cache()


class RevertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int


@config_history_api.get("")
async def list_config_keys() -> dict[str, Any]:
    """履歴対象 config key の一覧 (現 version + 版数)。UI のセレクタ用。"""
    keys: list[dict[str, Any]] = []
    for key, label in _KNOWN_KEYS.items():
        history = config_store.list_history(key, limit=_HISTORY_LIMIT)
        keys.append(
            {
                "key": key,
                "label": label,
                "current_version": history[0].version if history else None,
                "version_count": len(history),
            },
        )
    return {"keys": keys}


@config_history_api.get("/{key}")
async def get_config_history(key: str) -> dict[str, Any]:
    """1 key の版履歴 (新しい順、現行 version をマーク)。"""
    label = _require_known_key(key)
    history = config_store.list_history(key, limit=_HISTORY_LIMIT)
    current = history[0].version if history else None
    return {
        "key": key,
        "label": label,
        "current_version": current,
        "versions": [
            {
                "version": v.version,
                "note": v.note,
                "created_at": v.created_at,
                "is_current": v.version == current,
            }
            for v in history
        ],
    }


@config_history_api.get("/{key}/{version}")
async def get_config_version_value(key: str, version: int) -> dict[str, Any]:
    """特定 version の値 (閲覧・diff 用)。"""
    _require_known_key(key)
    value = config_store.get_config_version(key, version)
    if value is None:
        raise HTTPException(status_code=404, detail=f"version {version} が存在しません: {key}")
    return {"key": key, "version": version, "value": value}


@config_history_api.post("/{key}/revert")
async def revert_config(key: str, req: RevertRequest) -> dict[str, Any]:
    """指定 version の値を新 version として保存し直す (= 巻き戻し) + cache 無効化。

    過去の値をそのまま append するため履歴は失われない (revert も 1 版として残る)。
    READ_ONLY instance では middleware が先に 403 で弾く。
    """
    _require_known_key(key)
    value = config_store.get_config_version(key, req.version)
    if value is None:
        raise HTTPException(status_code=404, detail=f"version {req.version} が存在しません: {key}")
    new_version = config_store.save_config(
        key,
        value,
        note=f"v{req.version} へ巻き戻し",
    )
    _invalidate(key)
    _log.info("config_reverted", key=key, from_version=req.version, new_version=new_version)
    return {"reverted": True, "key": key, "from_version": req.version, "new_version": new_version}
