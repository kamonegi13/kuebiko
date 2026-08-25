"""DB 接続プールの状態を返す運用 API。

2026-08-25 の全停止 (`GET /api/v1/feed-options` の接続リーク) の切り分けに 1 時間
かかった理由が、**プールの内部状態を外から見る手段が無かった**こと。外形は

- `pg_stat_activity` の接続はほぼ 0 本 (漏れた接続は GC で PG 側だけ閉じるため)
- CPU 0% (全員 getconn 待ち)
- コンテナは healthy (health は DB を触らない)

となり、「DB は暇なのにアプリだけ止まる」ように見える。**判断は
``pool.get_stats()`` で行う** — pg_stat_activity だけを見ると原因を取り違える。

運用情報なので公開面には出さない (``READ_ONLY_GET_DENYLIST``)。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

db_pool_api = APIRouter(prefix="/api/v1/db-pool", tags=["ops"])

# 貸出中 (pool_size - pool_available) がこの割合を超えたら注意。リークなら
# 待ち (requests_waiting) が積み上がったまま減らない。
_BUSY_WARN_RATIO = 0.7


@db_pool_api.get("")
def get_db_pool_status() -> dict[str, Any]:
    """接続プールの統計。SQLite (dev/tests) では ``enabled: false`` を返す。

    ``leased`` が上限に張り付き ``waiting`` が減らない = 接続が返っていない
    (リーク)。負荷由来なら負荷が引いた時点で ``leased`` は下がる。
    """
    from src.storage.db_backend import is_pg_enabled

    if not is_pg_enabled():
        return {"enabled": False, "reason": "SQLite モード (DATABASE_URL 未設定)"}

    from src.storage.db_backend import _get_pool  # noqa: PLC0415 — 遅延 import で pool を作らせない

    stats: dict[str, Any] = _get_pool().get_stats()
    size = int(stats.get("pool_size", 0))
    available = int(stats.get("pool_available", 0))
    maximum = int(stats.get("pool_max", 0))
    leased = max(0, size - available)
    return {
        "enabled": True,
        # 読み手が最初に見る 3 つ。ここだけで枯渇か否かが分かる
        "leased": leased,
        "available": available,
        "max": maximum,
        "waiting": int(stats.get("requests_waiting", 0)),
        "saturated": bool(maximum and leased >= maximum * _BUSY_WARN_RATIO),
        # psycopg_pool の生統計 (connections_errors / requests_errors 等も含む)
        "raw": stats,
    }
