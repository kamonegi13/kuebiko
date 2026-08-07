"""routing rules の CRUD + 差分プレビュー API (案 B / Stage 2)。

GET  /api/v1/routing-rules          現行ルールセット + 語彙 + engine 有効状態
POST /api/v1/routing-rules          ルールセット保存 (検証 → DB config_store に版保存)
POST /api/v1/routing-rules/preview  代表シナリオで 現行(legacy) vs 提案 の channel 差分

write は READ_ONLY middleware (app.py) が 403 で block するため、ここで個別ガードは不要。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger

routing_rules_api = APIRouter(prefix="/api/v1/routing-rules", tags=["routing-rules"])

_log = get_logger(__name__)

_ARTICLE_TYPES = ["breaking", "recap", "tutorial", "opinion", "press", "research"]


class SaveRulesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[dict[str, Any]]


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[dict[str, Any]] | None = None  # None → 現行 file のルール


def _resolve_domain(domain: str) -> list[str]:
    """プロパティの値域 domain 名 → 実際の選択肢 (enum/set の値)。失敗しても編集は壊さない。"""
    if domain == "article_types":
        return list(_ARTICLE_TYPES)
    if domain == "importances":
        return ["high", "medium", "low"]
    if domain == "stances":
        return ["factual_report", "analytical", "opinion", "propaganda", "unknown"]
    if domain == "categories":
        from src.config_loader import KNOWN_ARTICLE_CATEGORIES

        return list(KNOWN_ARTICLE_CATEGORIES)
    if domain == "victim_sectors":
        try:
            from src.cti.taxonomy_normalizer import load_normalizer

            return sorted(load_normalizer().list_sector_canonical())
        except Exception as e:  # noqa: BLE001
            _log.warning("routing_vocab_sectors_failed", error=str(e))
            return []
    if domain == "actor_nations":
        try:
            from src.cti.actor_normalizer import load_actor_aliases

            return sorted({a.nation for a in load_actor_aliases().actors if a.nation})
        except Exception as e:  # noqa: BLE001
            _log.warning("routing_vocab_nations_failed", error=str(e))
            return []
    if domain == "keyword_lists":
        try:
            from src.cti.match_lists import get_match_lists

            return sorted(ml.name for ml in get_match_lists(force_reload=True))
        except Exception as e:  # noqa: BLE001
            _log.warning("routing_vocab_keyword_lists_failed", error=str(e))
            return []
    return []


@routing_rules_api.get("")
async def get_routing_rules() -> dict[str, Any]:
    """現行ルールセット + 型付きプロパティ・カタログ + engine 有効状態を返す。

    語彙統一 (2026-06-14): flag/記事属性 を撤廃し、編集 UI は properties カタログから
    完全に駆動される (各 property に kind/ops/values が付く)。
    """
    from src.cti.router import is_rules_engine_enabled
    from src.cti.routing_rules import describe_properties, load_routing_rules, valid_channels

    properties = [
        {**spec, "values": _resolve_domain(str(spec.get("domain", "")))}
        for spec in describe_properties()
    ]
    return {
        "rules": load_routing_rules(force_reload=True),
        "engine_enabled": is_rules_engine_enabled(),
        "vocabulary": {
            "channels": sorted(valid_channels()),
            # 型付きプロパティ・カタログ (id/kind/group/label/ops/values)。編集 UI の唯一の語彙源。
            "properties": properties,
            # category の in_config が参照できる config list 名。
            "config_lists": ["high_threat_brief_categories"],
        },
    }


@routing_rules_api.post("")
async def save_routing_rules(req: SaveRulesRequest) -> dict[str, Any]:
    """ルールセットを検証して DB (config_store) に保存。yaml/git は触らない。"""
    from src.config_loader import KNOWN_ARTICLE_CATEGORIES
    from src.cti.routing_rules import (
        CONFIG_KEY,
        invalidate_rules_cache,
        load_routing_rules,
        validate_routing_rules,
    )
    from src.storage.config_store import save_config

    errors = validate_routing_rules(req.rules, set(KNOWN_ARTICLE_CATEGORIES))
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors[:10]))

    version = save_config(CONFIG_KEY, req.rules, note="UI 編集")
    invalidate_rules_cache()
    return {
        "saved": True,
        "version": version,
        "count": len(load_routing_rules(force_reload=True)),
    }


@routing_rules_api.post("/preview")
async def preview_routing_rules(req: PreviewRequest) -> dict[str, Any]:
    """代表シナリオで「現行 legacy」vs「提案ルール engine」の channel を比較。

    DB 非依存・決定的。rules=None なら現行 file のルールでプレビュー。
    """
    from src.config_loader import KNOWN_ARTICLE_CATEGORIES
    from src.cti.router import _route_legacy, get_source_quality
    from src.cti.routing_rules import (
        PREVIEW_SCENARIOS,
        evaluate_routing_rules,
        load_routing_rules,
        validate_routing_rules,
    )
    from src.cti.routing_signals import RoutingSignals

    proposed = req.rules if req.rules is not None else load_routing_rules()
    errors = validate_routing_rules(proposed, set(KNOWN_ARTICLE_CATEGORIES))

    sq = get_source_quality()
    snap: dict[str, object] = {}
    results: list[dict[str, Any]] = []
    changed = 0
    for name, kw in PREVIEW_SCENARIOS:
        base: dict[str, Any] = {
            "source": "briefing",
            "importance": "medium",
            "article_type": "breaking",
        }
        base.update(kw)
        signals = RoutingSignals(**base)
        current = _route_legacy(signals, sq, snap)
        prop = evaluate_routing_rules(signals, sq, snap, rules=proposed)
        prop_channel = prop.channel if prop else "watch"
        prop_rule = prop.rule_id if prop else "(該当なし)"
        ch_changed = current.channel != prop_channel
        if ch_changed:
            changed += 1
        results.append(
            {
                "scenario": name,
                "current_channel": current.channel,
                "current_rule": current.rule_id,
                "proposed_channel": prop_channel,
                "proposed_rule": prop_rule,
                "changed": ch_changed,
            }
        )
    return {
        "scenarios": results,
        "changed_count": changed,
        "total": len(results),
        "errors": errors,
    }
