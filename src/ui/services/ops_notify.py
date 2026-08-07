"""親プロセス (UI/runner) 側からの ops チャンネル通知 (監査 2026-07-05 P2)。

従来の失敗通知は「run_pipeline 経路 × errors 非空 × ops webhook 生存」の 3 重 AND で
しか届かず、subprocess timeout / spawn 失敗 / runner crash / shutdown kill は
すべて無音だった。ここは **親プロセス側の通知 seam** — 子が通知できずに死んだ
失敗を運用者に届ける。子が正常系で自ら通知するケース (run_pipeline 完走) とは
重複しない (親は「子が報告できなかった失敗」のみ通知する)。
"""

from __future__ import annotations

import os

from src.logging_config import get_logger

_log = get_logger(__name__)


def _is_read_only() -> bool:
    return os.environ.get("READ_ONLY", "").strip() in ("1", "true", "yes")


def _is_notify_suppressed() -> bool:
    """通知を抑止すべき環境か (テスト実行 / 明示 disable)。

    2026-07-05 事故: post_ops_message は load_app_config() で実 .env の webhook を読むため、
    pipeline_runner の timeout テスト等が実 Discord ops を @here 付きでスパムしていた
    (「daily-briefing run 失敗 (runner 検知)」× 大量)。pytest は各テストで
    PYTEST_CURRENT_TEST を立てるので、それを検知して実投稿を構造的に封じる。
    CTI_DISABLE_OPS_NOTIFY=1 でも明示的に無効化できる (CI / dry-run 環境)。
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return os.environ.get("CTI_DISABLE_OPS_NOTIFY", "").strip() in ("1", "true", "yes")


async def post_ops_message(
    *,
    title: str,
    body: str,
    importance: str = "low",
    analyst_note: str | None = None,
) -> bool:
    """ops チャンネルに 1 通投稿する。失敗しても例外は上げない (False を返すのみ)。

    READ_ONLY instance / テスト実行では常に no-op。webhook 未設定運用も許容 (False)。
    """
    if _is_read_only() or _is_notify_suppressed():
        return False
    try:
        from src.config_loader import load_app_config
        from src.tools.channel_registry import resolve_webhooks
        from src.tools.discord_publisher import BriefingMessage, DiscordPublisher

        config = load_app_config()
        url = resolve_webhooks(config.discord_webhooks).get("ops")
        if not url:
            _log.info("ops_notify_skipped_no_webhook", title=title)
            return False
        msg = BriefingMessage(
            title=title,
            bluf=body,
            importance=importance,  # type: ignore[arg-type]
            category="system",
            summary=body,
            analyst_note=analyst_note,
            metadata={"target_channel": "ops"},
        )
        await DiscordPublisher(webhook_url=url).post(msg)
        return True
    except Exception as e:  # noqa: BLE001 — 通知失敗で呼び出し元 (runner/scheduler) を殺さない
        _log.warning("ops_notify_failed", title=title, error=str(e))
        return False


async def notify_pipeline_failure(pipeline_name: str, run_id: int, note: str) -> None:
    """runner (親) が検知した run 失敗を ops に通知する (子は通知できずに死んでいる)。"""
    await post_ops_message(
        title=f"🔴 {pipeline_name} run 失敗 (runner 検知)",
        body=f"@here {pipeline_name} 失敗 · {note} · run_id={run_id}",
        importance="high",
        analyst_note=note[:300],
    )


async def notify_pipeline_partial(pipeline_name: str, run_id: int, errors: list[str]) -> None:
    """完走したが errors ありの run (partial_failure) を ops に通知する。

    監査 backlog 2026-07-05: P2 の親側 seam は result file なしの異常死しか拾わず、
    「正常終了だが errors あり」は子が自前通知しない経路 (digest/synthesis 系) だと
    無音だった。呼び出し元 (runner) は result file の ops_notified=False の場合のみ
    ここに来る (run_pipeline 経路の自前通知との二重投稿はしない)。
    緊急ではないので @here は付けない。
    """
    first_error = errors[0][:300] if errors else None
    await post_ops_message(
        title=f"🟡 {pipeline_name} run 部分失敗 (runner 検知)",
        body=f"{pipeline_name} 部分失敗 · エラー {len(errors)} 件 · run_id={run_id}",
        importance="medium",
        analyst_note=first_error,
    )
