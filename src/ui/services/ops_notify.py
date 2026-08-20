"""親プロセス (UI/runner) 側からの ops チャンネル通知 (監査 2026-07-05 P2)。

従来の失敗通知は「run_pipeline 経路 × errors 非空 × ops webhook 生存」の 3 重 AND で
しか届かず、subprocess timeout / spawn 失敗 / runner crash / shutdown kill は
すべて無音だった。ここは **親プロセス側の通知 seam** — 子が通知できずに死んだ
失敗を運用者に届ける。子が正常系で自ら通知するケース (run_pipeline 完走) とは
重複しない (親は「子が報告できなかった失敗」のみ通知する)。

DB 永続化 (2026-08-21): 従来は Discord ops webhook へ送るだけで DB に残さず、
webhook 不達・未設定の警告が痕跡なく消えていた (access_audit と同じ教訓「stdout は
監査にならない」「成功も記録しないと沈黙の意味を決められない」に反する)。
``post_ops_message`` は webhook 送信の成否に関わらず ``ops_notices`` に必ず 1 行残す
(READ_ONLY / テスト実行時の no-op は従来どおり — 抑止は DB 書込にも及ぶ)。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.storage.run_history import RunHistoryRepository

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


def _persist_ops_notice(
    *,
    title: str,
    body: str,
    importance: str,
    sent: bool,
    repo: RunHistoryRepository | None = None,
) -> None:
    """ops_notices に 1 行残す。失敗しても例外は上げない (通知の送信そのものは止めない)。

    ``repo`` はテスト向けの永続化 seam — 本番経路では常に既定の ``RunHistoryRepository()``
    (= 稼働中の DB) に書く。access_audit の ``_record_audit`` と同型の fail-safe 方針。
    """
    try:
        from src.logging_config import mask_text
        from src.storage.run_history import RunHistoryRepository as _Repo

        # 例外文字列経由で webhook URL / メールが body に埋まる事故は read API 漏洩として
        # 2 度再発した型 — 保存時点でマスクする (DB は pg_dump バックアップにも残留するため、
        # API 応答側でなく書込側が正しい塞ぎ位置)。
        (repo or _Repo()).record_ops_notice(
            title=mask_text(title), body=mask_text(body), importance=importance, sent=sent
        )
    except Exception as e:  # noqa: BLE001 — 監査の書込失敗で通知経路を壊さない
        _log.warning("ops_notice_persist_failed", title=title, error=str(e))


async def post_ops_message(
    *,
    title: str,
    body: str,
    importance: str = "low",
    analyst_note: str | None = None,
    repo: RunHistoryRepository | None = None,
) -> bool:
    """ops チャンネルに 1 通投稿し、``ops_notices`` に永続化する。

    失敗しても例外は上げない (False を返すのみ)。READ_ONLY instance / テスト実行では
    常に no-op (DB にも残さない — 抑止は本来「実運用として起きなかったこと」を意味する)。
    webhook 未設定・送信失敗でも **DB には必ず 1 行残る** (webhook 不達が痕跡なく消えない
    ようにする、2026-08-21)。``repo`` はテストからの永続化 seam (省略時は既定 DB)。
    """
    if _is_read_only() or _is_notify_suppressed():
        return False
    sent = False
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
        sent = True
        return True
    except Exception as e:  # noqa: BLE001 — 通知失敗で呼び出し元 (runner/scheduler) を殺さない
        _log.warning("ops_notify_failed", title=title, error=str(e))
        return False
    finally:
        # webhook 未設定 (早期 return) / 送信成功 / 例外、いずれの経路でも必ず 1 行残す。
        _persist_ops_notice(title=title, body=body, importance=importance, sent=sent, repo=repo)


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
