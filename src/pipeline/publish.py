"""チャンネル解決 + Publisher 構築 + dry-run 表示ヘルパ (src.main から分割)。"""

from __future__ import annotations

import sys

from src.config_loader import AppConfig
from src.pipeline.summary import DiscordChannel
from src.tools.channel_registry import known_channel_ids, resolve_webhooks
from src.tools.discord_publisher import BriefingMessage, DiscordPublisher, Importance


def _resolve_channel(
    msg: BriefingMessage,
    importance_map: dict[Importance, DiscordChannel],
) -> DiscordChannel:
    """BriefingMessage から target_channel を決定する。

    優先順 (Phase 5K):
      1. ``msg.metadata['target_channel']`` (Grok / Inoreader が router で決定済の値)
      2. ``importance_map[msg.importance]`` (router 経由でない場合の安全網)

    衛生ゲート (2026-06-16): alert は最優先 (即応) channel。enriched importance=low の
    item は、Grok theme 等が alert を提案しても alert 不適とし importance ベースの
    default に降格する (降格のみ・昇格はしない)。Grok の theme→channel は値ある
    キュレーション (J1→japan_watch / F→alert 等、実測で content routing は再現しない)
    なので保持しつつ、theme 誤タグ (ジョーク→theme A=alert) が最優先 ch を汚染するのを
    importance で止める。「importance→channel の背骨 + 衛生層 (降格専用)」原則に整合。
    """
    target = msg.metadata.get("target_channel")
    if isinstance(target, str) and target in known_channel_ids():
        if target == "alert" and msg.importance == "low":
            return importance_map[msg.importance]
        return target
    return importance_map[msg.importance]


def _build_publishers(config: AppConfig) -> dict[DiscordChannel, DiscordPublisher]:
    """有効チャンネルの DiscordPublisher 群を作る (C1: レジストリ駆動)。

    webhook 解決は channel_registry.resolve_webhooks (DB レジストリ + built-in は
    AppConfig の .env 値、custom は環境変数 / .env)。URL 未設定 ch は作らない
    (投稿時に fallback_map で救済 or no_publisher エラー)。
    """
    publishers: dict[DiscordChannel, DiscordPublisher] = {}
    for channel, url in resolve_webhooks(config.discord_webhooks).items():
        if not url:
            continue
        publishers[channel] = DiscordPublisher(webhook_url=url)
    return publishers


def _print_dry_run(
    article_id: str,
    msg: BriefingMessage,
    importance_map: dict[Importance, DiscordChannel],
) -> None:
    """dry-run モードでの整形プレビュー (stdout)。

    Phase 5I: importance_map は run_pipeline 側で yaml ロード済の値を引数で渡す
    (定数フォールバックを廃止して SSoT を 1 本化)。
    """
    sys.stdout.write("\n" + "=" * 70 + "\n")
    sys.stdout.write(f"[{msg.importance.upper()} / {msg.category}] {msg.title}\n")
    sys.stdout.write(f"id: {article_id}\n")
    sys.stdout.write(f"  → channel: {_resolve_channel(msg, importance_map)}\n")
    sys.stdout.write("-" * 70 + "\n")
    sys.stdout.write(f"BLUF: {msg.bluf}\n\n")
    sys.stdout.write(f"{msg.summary}\n")
    if msg.iocs:
        sys.stdout.write(f"\nIOCs: {', '.join(msg.iocs)}\n")
    if msg.mitre_techniques:
        sys.stdout.write(f"MITRE: {', '.join(msg.mitre_techniques)}\n")
    if msg.analyst_note:
        sys.stdout.write(f"Note: {msg.analyst_note}\n")
    sys.stdout.write("=" * 70 + "\n")
