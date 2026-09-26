"""台帳更新の排他 — 同じ台帳を 2 つの run が並行して評価しないようにする (2026-09-26)。

台帳を更新する経路は 2 つある: 朝夕の定時 run (``synthesis/grounded/pipeline.py``、子プロセス)
と毎時の増分再評価 (``ui/services/ledger_reassess_job.py``、app プロセス)。どちらも
``build_estimate_stateful`` で「証拠を読む → ACH → revision を書く」を行う。並行すると、
先に読み始めた側が後から書き、**古い時点の証拠で出した判定が最新の revision になる**。
実測: 09-21〜25 に 24 件 (20 台帳)、毎回ちょうど毎時再評価の cap と同じ 6 件ずつ
(スリープ明けに定時 run と毎時段が同時に走った回を含む)。

プロセスをまたぐので PG の session advisory lock を使う。専用の接続で取り、pool には
戻さない (session lock は接続に付くため、pool に戻すと別の処理が lock を持ったままになる)。
プロセスが落ちれば接続ごと lock が外れるので、取り残しの掃除は要らない。
SQLite (dev/tests) では排他しない (単一プロセス前提)。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog

from src.storage import db_backend

_log = structlog.get_logger(__name__)

#: advisory lock の鍵 (int64)。台帳更新専用。他の用途と共有しない
LEDGER_LOCK_KEY = 0x6B75_6562_6C65_6467  # "kuebledg"
#: 待つ側の再試行間隔
POLL_SECONDS = 5.0


def _connect() -> Any:
    import psycopg  # PG モードでだけ使う (SQLite-only 環境に依存させない)

    return psycopg.connect(db_backend.get_database_url(), autocommit=True)


def _try_lock(conn: Any) -> bool:
    row = conn.execute("SELECT pg_try_advisory_lock(%s)", (LEDGER_LOCK_KEY,)).fetchone()
    return bool(row and row[0])


@asynccontextmanager
async def ledger_write_lock(*, wait_seconds: float = 0.0, holder: str) -> AsyncIterator[bool]:
    """台帳更新の lock を取る。取れたら True、取れなかったら False を yield する。

    ``wait_seconds=0`` は 1 回だけ試す (毎時段向け: 取れなければ今回は飛ばす)。
    正の値ならその秒数まで待つ (定時 run 向け: 毎時段は数分で終わる)。
    取れなかったときにどうするかは呼出側が決める。
    """
    if not db_backend.is_pg_enabled():
        yield True
        return
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001 — 接続できないなら排他できない。判断は呼出側
        _log.warning("ledger_lock_connect_failed", holder=holder, error=type(exc).__name__)
        yield False
        return
    acquired = False
    try:
        deadline = time.monotonic() + wait_seconds
        acquired = _try_lock(conn)
        while not acquired and time.monotonic() < deadline:
            await asyncio.sleep(POLL_SECONDS)
            acquired = _try_lock(conn)
        _log.info("ledger_lock", holder=holder, acquired=acquired, waited_max=wait_seconds)
        yield acquired
    finally:
        try:
            if acquired:
                conn.execute("SELECT pg_advisory_unlock(%s)", (LEDGER_LOCK_KEY,))
        finally:
            conn.close()
