"""model_tiers の DB 読み込み失敗の警報 (TIER-LOAD-ALARM)。

DB の読み込みに失敗すると解決は ``BUILTIN_MODEL_TIERS`` へ落ちる。出力は変えないが
**黙って落ちない**: プロセス内で数え (health-status が読む)、ops へは 1 日 1 回通知する。
ログ ``model_tiers_db_load_failed`` は呼び出し側 (model_tiers) が出す。
"""

from __future__ import annotations

import asyncio
from datetime import date

_failure_count = 0
_last_error = ""
_last_notified: list[date] = []
_pending_notices: set[asyncio.Task[bool]] = set()

_ERROR_MAX_CHARS = 200


def record_tier_load_failure(error: str) -> None:
    """失敗を数え、ops へ 1 日 1 回通知する (同期文脈ではログと計数だけ)。"""
    global _failure_count, _last_error
    _failure_count += 1
    _last_error = error[:_ERROR_MAX_CHARS]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if not _should_notify_today(date.today()):
        return
    from src.ui.services.ops_notify import post_ops_message

    task = loop.create_task(
        post_ops_message(
            title="model_tiers の DB 読み込みに失敗しました",
            body=f"コード既定の割当に落ちています: {_last_error}",
            importance="medium",
        )
    )
    _pending_notices.add(task)
    task.add_done_callback(_pending_notices.discard)


def _should_notify_today(today: date) -> bool:
    if _last_notified and _last_notified[-1] == today:
        return False
    _last_notified.append(today)
    del _last_notified[:-1]
    return True


def tier_load_failure_state() -> dict[str, object]:
    """health-status 用。``count`` が 0 でなければ DB の割当を読めていない。"""
    return {"count": _failure_count, "last_error": _last_error}


def reset_for_tests() -> None:
    global _failure_count, _last_error
    _failure_count = 0
    _last_error = ""
    _last_notified.clear()
