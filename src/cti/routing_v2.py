"""M3b: 重要度の再設計 (``src/cti/importance_v2.py``) に基づく新ルール。

``M3B_RULES`` は ``src/cti/routing_rules.py`` の ``evaluate_routing_rules(rules=...)`` が
そのまま評価できる形式 (config 駆動エンジンと同じ dict ルール列)。

2026-10-10: 利用者判断により、本番 Discord routing を DB ruleset v12 からこの
``M3B_RULES`` (= v13) へ切替える。``vuln`` (旧称 ``exploited``) は新規チャンネル
(未登録なら ``src/tools/channel_registry.py`` の built-in または DB に登録が必要)。
投入は ``scripts/apply_m3b_routing_rules.py`` (既定 dry-run、``--apply`` で DB 保存)。
"""

from __future__ import annotations

from typing import Any

#: 評価順 = first-match。src/cti/routing_rules.py の PROPERTY_CATALOG (severity /
#: severity_basis / jp / relevant / victim_sector / article_type) を参照する。
M3B_RULES: list[dict[str, Any]] = [
    {
        "id": "V2.alert_defense_targeted",
        "label": "日本の防衛セクターが標的 (深刻さ不問)",
        "channel": "alert",
        # 日本の防衛機関が標的の記事は深刻さを問わず alert へ (利用者判断 2026-10-10)。
        "when": {
            "all": [
                {"property": "jp", "op": "eq", "value": "targeted"},
                {"property": "victim_sector", "op": "eq", "value": "defense"},
            ]
        },
    },
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
