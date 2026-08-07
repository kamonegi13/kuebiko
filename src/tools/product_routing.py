"""プロダクト配信ルーティング (通知再設計: editorial product → channel を config 駆動化)。

morning/evening brief・weekly recap・status synthesis・PIR spotlight 等の **curated product**
の配信先 channel を DB (config_store key "product_routing") で管理し、情報フローで編集可能にする。

記事は routing rules engine が条件マッチで channel を決めるが、product は条件でなく
「名前 → channel」の単純写像なので別 config として持つ (rule engine には載せない)。
配信の medium (push / web-only) は記事と共通で **channel の push 属性**が決める
(product もそれを尊重する) → 配信制御が情報フローに一本化される。

DB 未投入 / 障害時は built-in 既定に fail-safe (channel_registry / routing_rules と同パターン)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

PRODUCT_ROUTING_CONFIG_KEY = "product_routing"

# product id → 既定 channel id。情報フローで上書き可。
# 既定は現行の配信先を踏襲 (brief=push 維持)。spotlight は watch (= push 属性に従い web-only)。
BUILTIN_PRODUCT_ROUTING: dict[str, str] = {
    "morning_brief": "brief",
    "evening_brief": "brief",
    "weekly_recap": "brief",
    "status_synthesis": "brief",
    "pir_spotlight": "watch",
}

# 未知 product / 未設定時の安全側 fallback。web-only な tier に落とし誤爆 push を防ぐ。
_FALLBACK_CHANNEL = "watch"

# load_product_routing のプロセス内キャッシュ (channel_registry と同パターン)。
_CACHE: dict[str, dict[str, str]] = {}


def load_product_routing(*, db_path: Path | None = None) -> dict[str, str]:
    """product → channel マッピングを返す。**DB (config_store) を正**とし built-in に fail-safe。

    built-in を base に DB 値で上書き (新 product 追加時に DB 未投入でも既定で動く)。
    """
    cache_key = str(db_path)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    routing = _load_uncached(db_path=db_path)
    _CACHE[cache_key] = routing
    return routing


def _load_uncached(*, db_path: Path | None) -> dict[str, str]:
    merged = dict(BUILTIN_PRODUCT_ROUTING)
    try:
        from src.storage.config_store import get_config

        raw = get_config(PRODUCT_ROUTING_CONFIG_KEY, db_path=db_path)
    except Exception as e:  # noqa: BLE001 — DB 障害で配信経路を壊さない
        _log.warning("product_routing_db_load_failed", error=str(e))
        return merged
    if not isinstance(raw, dict):
        return merged
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and v:
            merged[k] = v
    return merged


def product_channel(product_id: str, *, db_path: Path | None = None) -> str:
    """product の配信先 channel id。未知 product は web-only tier に安全側 fallback。"""
    return load_product_routing(db_path=db_path).get(product_id, _FALLBACK_CHANNEL)


def invalidate_product_routing_cache() -> None:
    """UI 保存後などにキャッシュを破棄する (次回 load で DB を読み直す)。"""
    _CACHE.clear()


def seed_product_routing_if_absent(*, db_path: Path | None = None) -> bool:
    """DB 未投入なら built-in マッピングを version 1 として取り込む (起動時)。"""
    from src.storage.config_store import seed_config_if_absent

    return seed_config_if_absent(
        PRODUCT_ROUTING_CONFIG_KEY,
        dict(BUILTIN_PRODUCT_ROUTING),
        note="初期 seed (editorial product routing)",
        db_path=db_path,
    )


def validate_product_routing(raw: Any, *, known_channels: set[str]) -> list[str]:
    """保存前検証。エラー文字列の list を返す (空 = OK)。

    Args:
        raw: 保存しようとする product → channel の dict。
        known_channels: 既知 channel id の集合 (存在しない channel への配信を防ぐ)。
    """
    if not isinstance(raw, dict) or not raw:
        return ["product_routing は空でない dict である必要があります"]
    errs: list[str] = []
    for k, v in raw.items():
        if not isinstance(k, str) or not k:
            errs.append(f"product id が不正です: {k!r}")
            continue
        if not isinstance(v, str) or not v:
            errs.append(f"product '{k}': channel が必要です")
        elif v not in known_channels:
            errs.append(f"product '{k}': channel '{v}' が存在しません")
    return errs
