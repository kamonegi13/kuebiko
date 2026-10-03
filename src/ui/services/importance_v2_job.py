"""重要度の再設計の記録を毎時付ける (2026-10-03、記録のみの段 M1・M2)。

深刻度の軸を付ける毎時の段の直後に呼ぶ (軸が付いた記事だけが対象)。決まりごとだけで LLM を
使わないので軽い。版 (``RULE_VERSION``) を上げると、窓の中の古い版の記録を付け直す。

停止: ``IMPORTANCE_V2_RECORD=0``。設計: docs/importance_relevance_redesign.md。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from src.cti.importance_v2 import RULE_VERSION, derive

_log = structlog.get_logger(__name__)

_FLAG = "IMPORTANCE_V2_RECORD"
#: 窓。比較の画面 (30 日) を覆い、版を上げたときに付け直す範囲
LOOKBACK_DAYS = 35
#: 1 回の上限 (決まりごとだけなので数百件でも 1 秒程度)
PER_RUN_LIMIT = 1000


def record_enabled() -> bool:
    return os.environ.get(_FLAG, "1") != "0"


def record_importance_v2(repo: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """未記録・古い版の記事に導出結果を記録する。戻り値は run_history に載せる要約。"""
    if not record_enabled():
        return {"skipped": "flag_off"}
    since = ((now or datetime.now(UTC)) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    inputs = repo.pending_importance_inputs(
        since=since, rule_version=RULE_VERSION, limit=PER_RUN_LIMIT
    )
    for aid, inp in inputs.items():
        repo.save_importance_v2(aid, derive(inp))
    _log.info("importance_v2_recorded", count=len(inputs), rule_version=RULE_VERSION)
    return {"recorded": len(inputs), "rule_version": RULE_VERSION}
