"""GPU の枠の道具 4 本 (PSTOP・VERIFY・DAY0・PAIR) の共通部品。標準ライブラリのみ。"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
DEFAULT_API = "http://127.0.0.1:8001"
HTTP_TIMEOUT_S = 15.0


def next_clock(now: datetime, hhmm: str) -> datetime:
    """``now`` 以降で最初の JST の HH:MM (壁時計の締め切り)。"""
    hour, minute = (int(x) for x in hhmm.split(":"))
    target = now.astimezone(JST).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target >= now else target + timedelta(days=1)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def http_json(
    method: str, url: str, body: dict[str, Any] | None = None, timeout: float = HTTP_TIMEOUT_S
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - 固定のローカル http 先のみ
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def api_busy_jobs(jobs: list[dict[str, Any]]) -> list[str]:
    """GET /api/v1/jobs の view から、いま走っているジョブ id。"""
    out: list[str] = []
    for j in jobs:
        last = j.get("last_run") or {}
        if j.get("running_since") or last.get("status") == "running":
            out.append(str(j.get("id")))
    return out
