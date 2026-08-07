"""ルーティング判定結果 (Phase 5L-1)。

router.route() の戻り値を構造化した。channel と「どの rule で / どの signal が
match して決まったか」を結果に同梱することで、誤分類調査時に grep だけで原因
特定できるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# C1 (チャンネル config-driven 化): Literal 6 固定 → str に緩和。
# 有効チャンネルの SSoT は src/tools/channel_registry.py (routing rules の
# 投稿先検証は routing_rules.validate_routing_rules がレジストリ照合で行う)。
DiscordChannel = str


@dataclass(frozen=True)
class RoutingDecision:
    """route() の判定結果 + 説明。

    属性:
        channel: 決定された Discord チャンネル
        rule_id: マッチした規則 ID (例: "R2.inoreader.alert_japan_critical_apt")
        reason: 判定理由 (人向け短文、ログ・UI 用)
        signals_snapshot: 判定に用いた主要シグナル (デバッグ用、空 dict でも可)
    """

    channel: DiscordChannel
    rule_id: str
    reason: str
    signals_snapshot: dict[str, object] = field(default_factory=dict)
