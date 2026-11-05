"""スリープ (時計の飛び) を持ち時間から除く絶対 timeout (2026-11-05)。

背景: ホストがスリープすると Docker の VM が止まり、復帰後に VM の単調時計が飛ぶ。
``asyncio.wait_for`` はその飛びを経過時間に数えるため、実際は数分しか動いていない子が
一括で timeout になる (11-05 の朝ブリーフ)。

方式: 短い刻み (``tick``) で待ち、待った時間より実測の経過が ``jump_threshold`` 秒以上
長ければ「止まっていた時間」とみなして持ち時間に足す。実際に止まった処理 (出力が無く
刻みが正常に進む) は従来どおり持ち時間で打ち切られる。

モード (``PIPELINE_TIMEOUT_SLEEP_AWARE``): ``off`` = 従来の wait_for / ``shadow`` =
飛びを検知してログに出すだけ (打ち切りは従来どおり) / ``on`` = 延長する。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Literal

from src.logging_config import get_logger

_log = get_logger(__name__)

Mode = Literal["off", "shadow", "on"]

TICK_SECONDS = 15.0
#: 待った時間よりこれ以上長く経っていたら飛びとみなす (GC・負荷の遅れは数秒)
JUMP_THRESHOLD_SECONDS = 60.0
#: 1 run で足せる延長の合計の上限 (時計の異常で永久に生きないための歯止め)
MAX_EXTENSION_SECONDS = 6 * 3600.0


def resolve_mode() -> Mode:
    raw = os.environ.get("PIPELINE_TIMEOUT_SLEEP_AWARE", "off").strip().lower()
    if raw in ("1", "on", "true"):
        return "on"
    if raw == "shadow":
        return "shadow"
    return "off"


async def wait_with_jump_allowance[T](
    task: asyncio.Task[T],
    *,
    timeout: float,
    mode: Mode,
    run_id: int = 0,
    tick: float = TICK_SECONDS,
    jump_threshold: float = JUMP_THRESHOLD_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """``task`` の完了を待つ。持ち時間を超えたら task を cancel して TimeoutError。"""
    start = last = clock()
    jumped_total = 0.0  # 検知した飛びの合計 (shadow でも数える)
    try:
        while True:
            applied = min(jumped_total, MAX_EXTENSION_SECONDS) if mode == "on" else 0.0
            remaining = timeout + applied - (clock() - start)
            if remaining <= 0:
                if jumped_total > 0:
                    _log.warning(
                        "pipeline_timeout_with_clock_jump",
                        run_id=run_id,
                        mode=mode,
                        jumped_seconds=round(jumped_total),
                        applied_seconds=round(applied),
                    )
                raise TimeoutError
            waited = min(tick, remaining)
            done, _ = await asyncio.wait({task}, timeout=waited)
            if done:
                return task.result()
            now = clock()
            gap = (now - last) - waited
            last = now
            if gap >= jump_threshold:
                jumped_total += gap
                _log.warning(
                    "pipeline_clock_jump",
                    run_id=run_id,
                    mode=mode,
                    gap_seconds=round(gap),
                    jumped_total_seconds=round(jumped_total),
                )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
