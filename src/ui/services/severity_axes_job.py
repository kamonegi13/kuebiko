"""深刻度の軸を毎時付ける (毎時保守チェーンの段、2026-09-25)。

detect (朝夕) の時点で軸が揃っているよう、配信済み high/medium の記事に先回りで付ける。
detect 側にも穴埋め (上限 40) はあるが、朝夕の run でまとめて付けると数分かかるため
毎時に分散させる。1 件 ~3 秒 (s17) × 上限 40 ≈ 1 分 (並列 2)。

停止: ``SEVERITY_AXES_HOURLY=0``。上限: ``SEVERITY_AXES_HOURLY_CAP`` (既定 40)。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

_FLAG = "SEVERITY_AXES_HOURLY"
_CAP_ENV = "SEVERITY_AXES_HOURLY_CAP"
DEFAULT_CAP = 40
#: 対象の窓。detect の候補窓 (24h) より広く取り、止まっていた時間の取りこぼしを拾う
LOOKBACK_HOURS = 72

_SQL = """
SELECT a.article_id, a.title, a.summary
FROM articles a
WHERE a.status = 'posted' AND a.importance IN ('high', 'medium')
  AND a.created_at >= ?
  AND NOT EXISTS (SELECT 1 FROM article_severity_axes s WHERE s.article_id = a.article_id)
ORDER BY a.created_at DESC
LIMIT ?
"""


def hourly_axes_enabled() -> bool:
    return os.environ.get(_FLAG, "1") != "0"


def hourly_axes_cap() -> int:
    raw = os.environ.get(_CAP_ENV, str(DEFAULT_CAP)).strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_CAP


def pending_articles(
    repo: Any, *, cap: int, now: datetime | None = None
) -> list[tuple[str, str, str]]:
    """軸が無い配信済み high/medium を新しい順に最大 cap 件 (同一 id の重複行は 1 件に)。"""
    since = ((now or datetime.now(UTC)) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の接続 seam 共有
        rows = conn.execute(_SQL, (since, cap * 2)).fetchall()
    seen: dict[str, tuple[str, str, str]] = {}
    for r in rows:
        aid = str(r["article_id"])
        seen.setdefault(aid, (aid, str(r["title"] or ""), str(r["summary"] or "")))
    return list(seen.values())[:cap]


async def run_severity_axes_hourly() -> dict[str, Any]:
    """毎時ジョブの入口。戻り値は run_history に載せる要約。"""
    if not hourly_axes_enabled():
        _log.info("severity_axes_hourly_disabled")
        return {"skipped": "flag_off"}
    from src.config_loader import load_app_config
    from src.cti.severity_axes import classify_axes
    from src.storage.run_history import RunHistoryRepository
    from src.synthesis.grounded.detect_ml import ensure_axes
    from src.tools.model_tiers import Step, build_llm_for

    repo = RunHistoryRepository()
    cap = hourly_axes_cap()
    todo = pending_articles(repo, cap=cap)
    if not todo:
        return {"pending": 0, "saved": 0}
    llm = build_llm_for(Step.SEVERITY_AXES, load_app_config())

    async def _classify(title: str, summary: str) -> dict[str, str] | None:
        axes = await classify_axes(llm, title, summary)
        return axes.model_dump() if axes is not None else None

    saved = await ensure_axes(
        repo, todo, _classify, model_label=str(getattr(llm, "model", "")), limit=cap
    )
    _log.info("severity_axes_hourly_done", pending=len(todo), saved=saved)
    return {"pending": len(todo), "saved": saved}
