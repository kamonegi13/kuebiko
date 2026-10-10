"""M3b 段1: 重要度の再設計 (``src/cti/importance_v2.py``) に基づく新ルール (shadow 専用)。

``M3B_RULES`` は ``src/cti/routing_rules.py`` の ``evaluate_routing_rules(rules=...)`` が
そのまま評価できる形式 (config 駆動エンジンと同じ dict ルール列)。本番の投稿先決定には使わない
— ``_build_briefing`` が現行 v12 ルールの判定 (``route()``) の**後**に、同じ signals から
この新ルールを評価して ``routing_v2_shadow`` に記録する (記録のみ)。

``vuln`` (旧称 ``exploited``) は新規チャンネル (未登録)。``validate_routing_rules`` は
shadow 経路では呼ばない — 評価 (``evaluate_routing_rules``) 自体は channel の存在を
確認しないため、未登録でも shadow 記録は問題なく動く。``vuln`` channel を
``src/tools/channel_registry.py`` の built-in (または DB) に登録すれば
``validate_routing_rules(M3B_RULES, ...)`` も通るようになる (詳細はモジュール docstring 末尾)。

想定の channel 登録 (将来、本番投入時): id=``vuln``、label="VULN（悪用中の脆弱性）"、
webhook_env_key=``DISCORD_WEBHOOK_VULN``、fallback=``watch``。
"""

from __future__ import annotations

from typing import Any

#: 評価順 = first-match。src/cti/routing_rules.py の PROPERTY_CATALOG (severity /
#: severity_basis / jp / relevant / article_type) を参照する。
M3B_RULES: list[dict[str, Any]] = [
    {
        "id": "V2.alert",
        "label": "深刻 (S3) かつ関連性あり",
        "channel": "alert",
        "when": {
            "all": [
                {"property": "severity", "op": "eq", "value": "S3"},
                {"property": "relevant", "op": "is_true"},
                {
                    "property": "article_type",
                    "op": "not_in",
                    "value": ["recap", "tutorial", "opinion", "press"],
                },
            ]
        },
    },
    {
        "id": "V2.vuln",
        "label": "深刻 (S3) の悪用中の脆弱性 (関連性なし)",
        "channel": "vuln",
        "when": {
            "all": [
                {"property": "severity", "op": "eq", "value": "S3"},
                {"property": "relevant", "op": "is_false"},
                {
                    "property": "severity_basis",
                    "op": "in",
                    "value": ["kev", "exploited", "exploited_product"],
                },
            ]
        },
    },
    {
        "id": "V2.japan_watch",
        "label": "日本が標的・被害",
        "channel": "japan_watch",
        "when": {"property": "jp", "op": "in", "value": ["targeted", "affected"]},
    },
    {
        "id": "V2.fallback",
        "label": "フォールバック",
        "channel": "watch",
        "when": {"always": True},
    },
]

__all__ = ["M3B_RULES"]
