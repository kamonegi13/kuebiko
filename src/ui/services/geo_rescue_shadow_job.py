"""地政学の救済の判定 v2 の影の記録の判定の段 (毎時保守チェーンの最後、2026-10-31)。

triage の時点で写した ``pending`` の行を、古い順に上限の件数
(``GEO_RESCUE_SHADOW_PER_RUN``、既定 30) と時間の予算 (``GEO_RESCUE_SHADOW_BUDGET_SECONDS``、
既定 150 秒) まで判定する。残りは次の回へ持ち越し、
72 時間を過ぎた行は ``expired`` にする (``pipeline/geo_rescue_shadow.judge_pending``)。

- pir の判定のモデル = ``Step.TRIAGE_GEO_RESCUE`` (本番の救済と同じ割当、いま 26b)、問いのモデル =
  ``Step.GEO_RESCUE_JUDGE`` の解決先 (救済の割当を継承)。どちらも名前と digest を行に記録する
- 旗 ``GEO_RESCUE_JUDGED_SHADOW`` が 0 なら何もしない。失敗は段の失敗として記録されるだけで、
  チェーンの他の段には影響しない (段は最後に置く)
- think は本番の救済と同じ既定 (``ProcessorConfig.think_enabled`` の既定 False)
"""

from __future__ import annotations

from typing import Any

import structlog

_log = structlog.get_logger(__name__)


async def run_geo_rescue_shadow_hourly() -> dict[str, Any]:
    """毎時ジョブの入口。戻り値は run_history に載せる要約 (件数だけ)。"""
    from src.pipeline.geo_rescue_shadow import (
        JudgeContext,
        is_geo_rescue_shadow_enabled,
        judge_pending,
        model_digests,
    )

    if not is_geo_rescue_shadow_enabled():
        _log.info("geo_rescue_shadow_hourly_disabled")
        return {"skipped": "flag_off"}
    from src.config_loader import load_app_config
    from src.storage.run_history import RunHistoryRepository
    from src.tools.model_tiers import Step, build_llm_for, resolve_step_model

    config = load_app_config()
    rescue_llm = build_llm_for(Step.TRIAGE_GEO_RESCUE, config)
    model = str(getattr(rescue_llm, "model", "") or "") or None
    judge_model = resolve_step_model(Step.GEO_RESCUE_JUDGE)
    digests = await model_digests(config.ollama_base_url, [model, judge_model])
    ctx = JudgeContext(
        rescue_llm=rescue_llm,
        config=config,
        think=False,
        model=model,
        model_digest=digests.get(model) if model else None,
        judge_model=judge_model,
        judge_model_digest=digests.get(judge_model),
    )
    return dict(await judge_pending(RunHistoryRepository(), ctx))


__all__ = ["run_geo_rescue_shadow_hourly"]
