"""replay の材料として延ばした retention の既定値を固定する (2026-10-29)。"""

from __future__ import annotations

import inspect

from src.assessment import ledger
from src.storage import (
    repo_alert_dedup_shadow,
    repo_eventnews,
    repo_routing_v2_shadow,
    repo_runs,
    repo_severity_c_shadow,
    repo_triage_shadow,
)
from src.storage.records import DEFAULT_LOG_RETENTION_DAYS
from src.ui.services import maintenance


def _default_days(func: object) -> int:
    return int(inspect.signature(func).parameters["days"].default)  # type: ignore[arg-type]


def test_detection_log_and_semantic_skips_keep_365_days() -> None:
    assert ledger._DETECTION_LOG_RETENTION_DAYS == 365  # noqa: SLF001
    assert not hasattr(maintenance, "_DETECTION_LOG_RETENTION_DAYS")
    assert maintenance._SEMANTIC_SKIPS_RETENTION_DAYS == 365  # noqa: SLF001
    assert _default_days(repo_eventnews.EventNewsMixin.purge_semantic_skips) == 365


def test_run_logs_and_job_run_log_keep_180_days() -> None:
    assert DEFAULT_LOG_RETENTION_DAYS == 180
    assert _default_days(repo_runs.RunsMixin.purge_old_job_run_log) == 180


def test_shadow_tables_keep_180_days() -> None:
    assert repo_alert_dedup_shadow.DEFAULT_RETENTION_DAYS == 180
    assert repo_routing_v2_shadow.DEFAULT_RETENTION_DAYS == 180
    assert repo_severity_c_shadow.DEFAULT_RETENTION_DAYS == 180
    assert maintenance._TRIAGE_SHADOW_RETENTION_DAYS == 180  # noqa: SLF001
    assert maintenance._ROUTING_V2_SHADOW_RETENTION_DAYS == 180  # noqa: SLF001
    assert maintenance._ALERT_DEDUP_SHADOW_RETENTION_DAYS == 180  # noqa: SLF001
    assert maintenance._SEVERITY_C_SHADOW_RETENTION_DAYS == 180  # noqa: SLF001
    assert _default_days(repo_triage_shadow.TriageShadowMixin.purge_triage_shadow) == 180


def test_llm_usage_keeps_365_days() -> None:
    assert maintenance._LLM_USAGE_RETENTION_DAYS == 365  # noqa: SLF001
