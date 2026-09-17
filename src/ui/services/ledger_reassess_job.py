"""台帳の増分再評価 (毎時、開設なし) — 更新の繰越をなくす (2026-09-17、利用者判断)。

朝夕の定時 run は再評価 cap 12 で毎 run 20-75 件を繰越していた (backlog 30-87)。
「更新すべき台帳は全部更新される」を満たすため、毎時保守チェーンの段として
小 cap (既定 6 ≈ ACH 32 秒 × 6 ≈ 3 分) の増分 ACH を回す。**新規開設はしない**
(開設判断は候補全体を見る朝夕の run に残す)。従来の原則「収集イベント=割当のみ・
評価は定時」からの明示的な変更 (docs/research/llm_training/SYNTHESIS.md §47 追記 6)。

停止: ``LEDGER_REASSESS_HOURLY=0``。cap: ``LEDGER_REASSESS_HOURLY_CAP`` (既定 6)。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

_FLAG = "LEDGER_REASSESS_HOURLY"
_CAP_ENV = "LEDGER_REASSESS_HOURLY_CAP"
DEFAULT_CAP = 6


def hourly_reassess_enabled() -> bool:
    return os.environ.get(_FLAG, "1") != "0"


def hourly_reassess_cap() -> int:
    raw = os.environ.get(_CAP_ENV, str(DEFAULT_CAP)).strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_CAP


async def run_ledger_reassess_hourly() -> dict[str, Any]:
    """毎時ジョブの入口。戻り値は run_history に載せる要約。"""
    if not hourly_reassess_enabled():
        _log.info("ledger_reassess_hourly_disabled")
        return {"skipped": "flag_off"}
    from src.assessment.ledger import ledger_mode

    if ledger_mode() != "on":
        return {"skipped": "ledger_off"}
    from src.assessment.situation_store import SituationStore
    from src.assessment.stateful import build_estimate_stateful
    from src.config_loader import load_app_config
    from src.storage.run_history import RunHistoryRepository
    from src.tools.model_tiers import Step, build_llm_for

    cfg = load_app_config()
    cap = hourly_reassess_cap()
    db_path = Path("data/run_history.db")
    est = await build_estimate_stateful(
        llm=build_llm_for(Step.SYNTHESIS_ANALYSIS, cfg),
        fast_llm=build_llm_for(Step.SYNTHESIS_DETECT, cfg),
        period_type="daily",
        repo=RunHistoryRepository(),
        store=SituationStore(db_path=db_path),
        db_path=db_path,
        reassess_cap=cap,
        open_new=False,
    )
    moved = sum(1 for j in est.judgments if j.delta_type not in ("", "no_change"))
    _log.info("ledger_reassess_hourly_done", cap=cap, judgments=len(est.judgments), moved=moved)
    return {"cap": cap, "judgments": len(est.judgments), "moved": moved}
