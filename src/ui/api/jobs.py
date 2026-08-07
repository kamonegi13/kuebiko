"""背景ジョブ統一制御 API (2026-07-06)。

全背景ジョブ (K1 pipeline / K2 bespoke / K3 reactive) を 1 つの Job 抽象として
list / toggle / reschedule / trigger する。schedule/enabled は job_registry (DB SSoT)
に版保存され、稼働中スケジューラへ即反映される (再起動でも維持)。

write 系 (toggle/schedule/run) は READ_ONLY instance では middleware が 403 で block。
protection=critical の無効化はサーバ側でも confirm を要求する (ミス防止の二重防御)。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.logging_config import get_logger
from src.scheduler.job_registry import (
    JobDef,
    apply_schedule_to_scheduler,
    danger_window_note,
    danger_windows,
    get_job,
    load_jobs,
    set_job_enabled,
    update_job_schedule,
    validate_schedule,
)

_log = get_logger(__name__)

jobs_api = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


class ToggleRequest(BaseModel):
    enabled: bool
    confirm: bool = False  # critical 停止の明示確認 (ミス防止)


class ScheduleRequest(BaseModel):
    schedule_type: str | None = None
    hour: int | None = None
    minute: int | None = None
    day_of_week: str | None = None
    day: str | None = None
    interval_minutes: int | None = None
    offset_minutes: int | None = None
    debounce_hours: float | None = None


def _job_view(
    j: JobDef,
    *,
    scheduler: Any,
    last_bespoke: dict[str, dict[str, str]],
    last_pipeline: dict[str, dict[str, str]],
    all_jobs: list[JobDef],
) -> dict[str, Any]:
    """JobDef + ライブ状態 (next_run / paused / last_run) を 1 dict にまとめる。"""
    next_run = None
    is_paused = None
    if scheduler is not None and j.kind != "reactive":
        try:
            nr = scheduler.next_run_at(j.id)
            next_run = nr.isoformat() if nr else None
            is_paused = scheduler.is_paused(j.id)
        except Exception:  # noqa: BLE001 — job 未登録等は None
            pass
    last = last_bespoke.get(j.id) or last_pipeline.get(j.id)
    return {
        **j.model_dump(mode="json"),
        "schedule_label": j.schedule_label(),
        "next_run_at": next_run,
        "is_paused": is_paused,
        "last_run": last,
        "danger_note": danger_window_note(j, all_jobs),
    }


@jobs_api.get("")
async def list_jobs_endpoint(request: Request) -> dict[str, Any]:
    """全背景ジョブ + ライブ状態を返す (運用コンソール用)。"""
    scheduler = getattr(request.app.state, "scheduler", None)
    repo = request.app.state.repo
    try:
        last_bespoke = repo.get_job_last_runs()
    except Exception as e:  # noqa: BLE001
        _log.warning("job_last_runs_failed", error=str(e))
        last_bespoke = {}
    try:
        last_pipeline = repo.latest_runs_by_pipeline()
    except Exception as e:  # noqa: BLE001
        _log.warning("latest_runs_by_pipeline_failed", error=str(e))
        last_pipeline = {}
    jobs = load_jobs()
    view = [
        _job_view(
            j,
            scheduler=scheduler,
            last_bespoke=last_bespoke,
            last_pipeline=last_pipeline,
            all_jobs=jobs,
        )
        for j in jobs
    ]
    disabled_important = [
        j.id for j in jobs if not j.enabled and j.protection in ("critical", "important")
    ]
    return {
        "jobs": view,
        "scheduler_available": scheduler is not None,
        # ミス防止: critical/important が停止中なら UI が常設バナーで警告する
        "disabled_important": disabled_important,
        # 24h タイムラインの危険帯シェード (重い LLM ジョブの現在時刻から動的生成)。
        # 収集はこの区間に発火が被ると動的に抑止される (固定の解析帯は 2026-07-07 廃止)。
        "danger_windows": danger_windows(jobs),
    }


@jobs_api.get("/{job_id}/runs")
async def job_runs_endpoint(job_id: str, request: Request, limit: int = 20) -> dict[str, Any]:
    """選択ジョブの実行履歴を新しい順に返す (詳細パネルの一覧表示用)。"""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"ジョブが見つかりません: {job_id}")
    repo = request.app.state.repo
    try:
        runs = repo.runs_for_job(job_id, limit=limit)
    except Exception as e:  # noqa: BLE001
        _log.warning("job_runs_failed", job_id=job_id, error=str(e))
        runs = []
    return {"job_id": job_id, "kind": job.kind, "runs": runs}


@jobs_api.post("/{job_id}/toggle")
async def toggle_job(job_id: str, req: ToggleRequest, request: Request) -> dict[str, Any]:
    """ジョブの enabled を切替える (protection=critical の停止は confirm 必須)。"""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"ジョブが見つかりません: {job_id}")
    if not req.enabled and job.protection == "critical" and not req.confirm:
        raise HTTPException(
            status_code=409,
            detail=(
                f"「{job.title}」は重要なジョブです。停止すると: {job.disable_impact} "
                "続行するには確認が必要です。"
            ),
        )
    updated = set_job_enabled(job_id, req.enabled)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"ジョブが見つかりません: {job_id}")
    # 稼働中スケジューラへ反映 (reactive は trigger 側が enabled を見るので対象外)
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None and updated.kind != "reactive":
        try:
            if req.enabled:
                scheduler.resume(job_id=job_id)
            else:
                scheduler.pause(job_id=job_id)
        except Exception as e:  # noqa: BLE001
            _log.warning("job_toggle_apply_failed", job_id=job_id, error=str(e))
    _log.info("job_toggled", job_id=job_id, enabled=req.enabled)
    return {"ok": True, "job_id": job_id, "enabled": req.enabled}


@jobs_api.post("/{job_id}/schedule")
async def reschedule_job(job_id: str, req: ScheduleRequest, request: Request) -> dict[str, Any]:
    """ジョブのスケジュールを変更する (検証 + 危険時刻警告、live 反映)。"""
    current = get_job(job_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"ジョブが見つかりません: {job_id}")
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    candidate = current.model_copy(update=patch)
    err = validate_schedule(candidate)
    if err is not None:
        raise HTTPException(status_code=400, detail=err)
    updated = update_job_schedule(job_id, patch)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"ジョブが見つかりません: {job_id}")
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None and updated.kind != "reactive":
        try:
            apply_schedule_to_scheduler(scheduler, updated)
        except Exception as e:  # noqa: BLE001
            _log.warning("job_reschedule_apply_failed", job_id=job_id, error=str(e))
    _log.info("job_rescheduled", job_id=job_id, schedule=updated.schedule_label())
    return {
        "ok": True,
        "job_id": job_id,
        "schedule_label": updated.schedule_label(),
        "danger_note": danger_window_note(updated, load_jobs()),
    }


@jobs_api.post("/{job_id}/run")
async def run_job_now(job_id: str, request: Request) -> dict[str, Any]:
    """ジョブを今すぐ手動実行する (scheduler job のみ、reactive は不可)。"""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"ジョブが見つかりません: {job_id}")
    if job.kind == "reactive":
        raise HTTPException(
            status_code=400,
            detail="連動実行のジョブは手動で実行できません (収集後に自動で実行されます)",
        )
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="閲覧専用のため実行できません")
    try:
        run_at = scheduler.trigger_now(job_id)
    except Exception as e:  # noqa: BLE001
        _log.warning("job_trigger_failed", job_id=job_id, error=str(e))
        raise HTTPException(status_code=500, detail="実行の開始に失敗しました") from e
    _log.info("job_triggered_manually", job_id=job_id)
    return {"ok": True, "job_id": job_id, "triggered_at": run_at.isoformat()}
