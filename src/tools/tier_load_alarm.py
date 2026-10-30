"""model_tiers の DB 読み込み失敗の警報 (TIER-LOAD-ALARM)。

DB の読み込みに失敗すると解決は ``BUILTIN_MODEL_TIERS`` へ落ちる。出力は変えないが
**黙って落ちない**: プロセス内で数え (health-status が読む)、ops へは 1 日 1 回通知する。
ログ ``model_tiers_db_load_failed`` (詳細なエラーつき) は呼び出し側 (model_tiers) が出す。

⚠ 計数は**プロセス内**。API プロセスの health-status には API プロセスの失敗だけが出る。
pipeline の subprocess の失敗は、ログと ops 通知で知る (プロセス間の永続化は作らない)。
ops (Discord) へは例外の型名と固定文だけを送る (ホスト名・DB 名を含みうる本文は送らない)。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from datetime import date
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

HEALTH_SCOPE = "api_process"

_failure_count = 0
_last_error_type = ""
_last_notified: list[date] = []
_pending_notices: set[asyncio.Task[bool]] = set()

_NOTICE_TITLE = "model_tiers の DB 読み込みに失敗しました"
_NOTICE_BODY = "コード既定の割当に落ちています。詳細はログ model_tiers_db_load_failed を参照"


def record_tier_load_failure(error_type: str) -> None:
    """失敗を数え、ops へ 1 日 1 回通知する。例外は呼び出し元へ出さない。"""
    global _failure_count, _last_error_type
    try:
        _failure_count += 1
        _last_error_type = error_type[:100]
        _notify_once_a_day()
    except Exception as e:  # noqa: BLE001 — 警報の失敗で元の fallback 経路を壊さない
        _log.warning("tier_load_alarm_failed", error=type(e).__name__)


def _notify_once_a_day() -> None:
    today = date.today()
    if _last_notified and _last_notified[-1] == today:
        return
    if not _schedule_notice():
        return
    # 通知を実際に仕込めた後でだけ当日の枠を使う
    _last_notified.clear()
    _last_notified.append(today)


def _schedule_notice() -> bool:
    """どの文脈 (イベントループの有無・ワーカースレッド) からでも通知を仕込む。"""
    from src.ui.services.ops_notify import post_ops_message

    def _coro() -> Coroutine[Any, Any, bool]:
        return post_ops_message(title=_NOTICE_TITLE, body=_NOTICE_BODY, importance="medium")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        thread = threading.Thread(
            target=lambda: asyncio.run(_coro()),
            name="tier-load-alarm-notify",
            daemon=True,
        )
        thread.start()
        return True
    task = loop.create_task(_coro())
    _pending_notices.add(task)
    task.add_done_callback(_pending_notices.discard)
    return True


def tier_load_failure_state() -> dict[str, object]:
    """health-status 用。``count`` > 0 なら、この API プロセスで DB の割当を読めていない。"""
    return {"scope": HEALTH_SCOPE, "count": _failure_count, "last_error_type": _last_error_type}


def reset_for_tests() -> None:
    global _failure_count, _last_error_type
    _failure_count = 0
    _last_error_type = ""
    _last_notified.clear()
