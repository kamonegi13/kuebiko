"""PIR Spotlight pipeline runner。

config/pir.yaml の spotlight.enabled=true な PIR each に対して narrative 生成、
pir_spotlight 表に UPSERT。weekly cron / 手動 trigger 共通 entrypoint。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.assessment.context import build_assessment_context
from src.logging_config import get_logger
from src.pir.loader import load_pir_config
from src.pir.models import Pir
from src.spotlight.generator import _resolve_period, generate_spotlight
from src.spotlight.models import SpotlightPeriod
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)


@dataclass(frozen=True)
class SpotlightRunResult:
    """1 回の spotlight run の集計結果。"""

    generated: list[str] = field(default_factory=list)  # 生成成功 PIR id
    skipped_no_matches: list[str] = field(default_factory=list)  # 該当記事不足
    skipped_disabled: list[str] = field(default_factory=list)  # spotlight 無効
    errors: list[str] = field(default_factory=list)  # PIR id + error 詳細


async def run_pir_spotlights(
    *,
    llm: LLMClient,
    repo: RunHistoryRepository | None = None,
    period_type: SpotlightPeriod = "weekly",
    target_pir_ids: list[str] | None = None,
    now: datetime | None = None,
) -> SpotlightRunResult:
    """Spotlight pipeline 本体。

    Args:
        llm: Ollama client (主に ``OLLAMA_SPOTLIGHT_MODEL`` を model に指定)
        repo: 永続化用、None なら dry-run
        period_type: 生成 window (daily/weekly/monthly)
        target_pir_ids: 対象 PIR id list (None なら spotlight.enabled=true 全件)
        now: 期間計算の基準時刻 (テスト用、None で現時刻)

    Returns:
        SpotlightRunResult
    """
    cfg = load_pir_config()
    candidates: list[Pir] = []
    for p in cfg.priorities:
        if target_pir_ids is not None:
            if p.id not in target_pir_ids:
                continue
        else:
            if not p.spotlight.enabled:
                continue
        candidates.append(p)

    _log.info(
        "spotlight_run_start",
        period_type=period_type,
        candidates=len(candidates),
        model=llm.model,
    )

    generated: list[str] = []
    skipped_no_matches: list[str] = []
    skipped_disabled: list[str] = []
    errors: list[str] = []

    # 状態中心: 横断 assessment (国家相関/forecast/鮮度) を 1 run 1 回だけ構築し全 PIR で共有
    # (出力中心の独立再導出を排除)。窓は spotlight の対象期間と一致させる (時間軸の混在を避ける)。
    start, end, _ = _resolve_period(period_type, now)
    lookback_hours = int((end - start).total_seconds() / 3600)
    assessment = build_assessment_context(
        nation_window_days=max(1, round(lookback_hours / 24)),
        freshness_lookback_hours=lookback_hours,
        db_path=Path("data/run_history.db"),
    )

    for pir in candidates:
        if not pir.enabled:
            skipped_disabled.append(pir.id)
            continue
        try:
            record = await generate_spotlight(
                pir, llm=llm, period_type=period_type, now=now, assessment=assessment
            )
            if record is None:
                skipped_no_matches.append(pir.id)
                continue
            if repo is not None:
                repo.upsert_pir_spotlight(record)
            generated.append(pir.id)
        except Exception as e:  # noqa: BLE001
            err_msg = f"{pir.id}: {type(e).__name__}: {e}"
            errors.append(err_msg)
            _log.error("spotlight_pir_failed", pir_id=pir.id, error=str(e))

    _log.info(
        "spotlight_run_complete",
        generated=len(generated),
        skipped_no_matches=len(skipped_no_matches),
        skipped_disabled=len(skipped_disabled),
        errors=len(errors),
    )

    return SpotlightRunResult(
        generated=generated,
        skipped_no_matches=skipped_no_matches,
        skipped_disabled=skipped_disabled,
        errors=errors,
    )
