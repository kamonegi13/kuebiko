"""Phase 3 Synthesis: pipeline 完了後の staleness check による自動 trigger。

cron (毎日 07:30 + 19:30 JST) と組み合わせ、article 流入 spike にも追随する
リアルタイム近接更新を実現する。

**モデル**: 呼び出し側 (run_pipeline) が synthesis 専用 (Dense 31b) の client を渡す。
定時 cron と同じ品質モデルで生成し、24h synthesis のモデル混在 (旧: main 26b で
上書き) を解消する。31b は 1 回 5-8 分かかるため debounce は 6h に設定。

Trigger 判定:
    - 初回 (daily synthesis 未生成) → 常に trigger
    - 前回 daily synthesis から ``min_age_hours`` 未満 → skip (debounce)
    - 新着 article (前回 daily synthesis 以降) が ``min_article_delta`` 未満 → skip

run_pipeline の末尾から best-effort で呼ばれる想定。失敗しても呼び出し側を
壊さないため、すべての例外を log に流して False 返却に倒す。
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.synthesis.runner import run_status_synthesis
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

# debounce: 前回 synthesis から最低この時間は trigger しない。
# Dense 31b 採用 (品質統一) で 1 回 5-8 分かかるため、毎時 cron との衝突を抑えるべく
# 2h → 6h に延長。定時 cron (07:30/19:30 JST) と合わせ実質 ~6h ごとに 31b で更新。
DEFAULT_MIN_AGE_HOURS = 6.0
# delta: 前回 synthesis 以降の新着 article がこれ以上なら trigger
DEFAULT_MIN_ARTICLE_DELTA = 5


async def maybe_trigger_daily_synthesis(
    *,
    llm: LLMClient,
    repo: RunHistoryRepository,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    min_article_delta: int = DEFAULT_MIN_ARTICLE_DELTA,
    now: datetime | None = None,
    fast_llm: LLMClient | None = None,
    analysis_llm: LLMClient | None = None,
) -> bool:
    """staleness check + 必要なら daily synthesis を生成。

    Returns:
        bool: 実際に synthesis を生成・永続化したら True、skip / 失敗なら False。
    """
    base_now = now or datetime.now(UTC)
    # 統一ジョブ制御 (2026-07-06): registry で enabled/debounce を UI 制御可能に。
    # 失敗時は既定値で続行 (fail-safe)。
    try:
        from src.scheduler.job_registry import get_job

        jd = get_job("auto-trigger-synthesis")
        if jd is not None:
            if not jd.enabled:
                _log.info("synthesis_auto_trigger_skipped_disabled")
                return False
            if jd.debounce_hours:
                min_age_hours = jd.debounce_hours
    except Exception as e:  # noqa: BLE001
        _log.debug("auto_trigger_registry_read_failed", error=str(e))

    try:
        latest = repo.get_latest_synthesis(period_type="daily")
    except Exception as e:  # noqa: BLE001
        _log.warning("synthesis_auto_trigger_lookup_failed", error=str(e))
        return False

    if latest is not None:
        age = base_now - latest.generated_at
        if age < timedelta(hours=min_age_hours):
            _log.info(
                "synthesis_auto_trigger_skipped_debounce",
                last_generated_at=latest.generated_at.isoformat(),
                age_hours=round(age.total_seconds() / 3600, 2),
                min_age_hours=min_age_hours,
            )
            return False
        try:
            new_count = repo.count_posted_articles_since(latest.generated_at)
        except Exception as e:  # noqa: BLE001
            _log.warning("synthesis_auto_trigger_count_failed", error=str(e))
            return False
        if new_count < min_article_delta:
            _log.info(
                "synthesis_auto_trigger_skipped_delta",
                new_article_count=new_count,
                min_article_delta=min_article_delta,
            )
            return False
        _log.info(
            "synthesis_auto_trigger_firing",
            reason="staleness_threshold_exceeded",
            new_article_count=new_count,
            age_hours=round(age.total_seconds() / 3600, 2),
        )
    else:
        _log.info("synthesis_auto_trigger_firing", reason="first_run")

    # 状態中心 (SYNTHESIS_STATE=1): 収集イベントは**割当のみの軽い状態更新** (LLM ゼロ) に
    # 留め、評価 (増分 ACH) とレポート生成は定時 cron (06:30/19:30) だけが行う。
    # stateful フル synthesis (5-12 LLM 呼出) を収集 pipeline の尻尾で回すと 1800s 予算を
    # 構造的に超える (2026-07-04 実測: 16:30/18:00/19:30 の 3 連続 timeout の増幅要因)。
    from src.assessment.ledger import ledger_mode

    # 可観測性 (有機的結合監査 M5): auto-trigger は scheduler job でなく RSS run の内側で
    # ネスト実行されるため runs / job_run_log のどちらにも痕跡が残らなかった。実働した
    # 分岐だけ job_run_log に記録する (skip はログのみで十分。記録失敗は本処理を壊さない)。
    def _record(status: str, detail: str) -> None:
        with contextlib.suppress(Exception):
            repo.record_job_run("auto-trigger-synthesis", status=status, detail=detail)

    if ledger_mode() == "on":
        try:
            from pathlib import Path

            from src.assessment.situation_store import SituationStore
            from src.assessment.stateful import refresh_ledger_assignments

            db = Path("data/run_history.db")
            assigned = refresh_ledger_assignments(
                repo=repo, store=SituationStore(db_path=db), db_path=db, now=base_now
            )
            _log.info("synthesis_auto_trigger_ledger_only", assigned=assigned)
            _record("succeeded", f"ledger refresh assigned={assigned}")
        except Exception as e:  # noqa: BLE001
            _log.warning("synthesis_auto_trigger_refresh_failed", error=str(e))
            _record("failed", f"ledger refresh: {type(e).__name__}: {e}"[:200])
        return False

    try:
        result = await run_status_synthesis(
            llm=llm,
            repo=repo,
            period_types=("daily",),
            fast_llm=fast_llm,
            analysis_llm=analysis_llm,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "synthesis_auto_trigger_failed",
            error=f"{type(e).__name__}: {e}",
        )
        _record("failed", f"{type(e).__name__}: {e}"[:200])
        return False

    if result.errors:
        _log.warning("synthesis_auto_trigger_partial", errors=result.errors)
    _record(
        "succeeded" if not result.errors else "failed",
        f"daily synthesis generated={result.daily_generated} errors={len(result.errors)}",
    )
    return result.daily_generated
