"""Phase 3 Synthesis: pipeline runner。

cron / 自動 trigger から呼ばれる。

period_types で生成対象を指定:
- ("daily",)              : 24h window、リアルタイム近接更新用
- ("weekly", "monthly")   : 既存 (互換)、中期動向用
- ("daily", "weekly", "monthly") : 全部
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.synthesis.generator import generate_synthesis
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

_VALID_PERIODS = ("daily", "weekly", "monthly")


@dataclass(frozen=True)
class SynthesisRunResult:
    """生成結果。`generated` は {period_type: success_bool}。"""

    generated: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def daily_generated(self) -> bool:
        return self.generated.get("daily", False)

    @property
    def weekly_generated(self) -> bool:
        return self.generated.get("weekly", False)

    @property
    def monthly_generated(self) -> bool:
        return self.generated.get("monthly", False)


async def run_status_synthesis(
    *,
    llm: LLMClient,
    repo: RunHistoryRepository | None = None,
    period_types: tuple[str, ...] | None = None,
    include_monthly: bool = True,
    fast_llm: LLMClient | None = None,
    analysis_llm: LLMClient | None = None,
) -> SynthesisRunResult:
    """status synthesis pipeline。

    Args:
        llm: 主 LLM クライアント (narrative ティア、散文生成用)
        fast_llm: 入力の多い triage 用の高速 LLM (26B)。detect-new 等に使う。None なら llm を流用
        analysis_llm: grounded の構造化分析 (ACH/射影) 用 (reasoning ティア)。None なら llm を流用
        repo: 永続化用 (None なら dry-run)
        period_types: 生成対象 period_type の tuple。None なら legacy 既定
            (include_monthly=True で ("weekly", "monthly"), False で ("weekly",))。
        include_monthly: 後方互換用、period_types 指定時は無視。

    Returns:
        SynthesisRunResult: 各 period_type の生成成否と error list。
    """
    if period_types is None:
        period_types = ("weekly", "monthly") if include_monthly else ("weekly",)
    # 不正な period_type を除外
    period_types = tuple(p for p in period_types if p in _VALID_PERIODS)

    generated: dict[str, bool] = {}
    errors: list[str] = []

    for period_type in period_types:
        res = await generate_synthesis(
            llm=llm, period_type=period_type, fast_llm=fast_llm, analysis_llm=analysis_llm
        )
        if res.record is not None and repo is not None:
            try:
                repo.upsert_status_synthesis(res.record)
                generated[period_type] = True
                _log.info("synthesis_persisted", period_type=period_type)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{period_type} persist: {type(e).__name__}: {e}")
        elif res.error:
            errors.append(f"{period_type} generate: {res.error}")

    return SynthesisRunResult(generated=generated, errors=errors)
