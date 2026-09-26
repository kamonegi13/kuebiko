"""背景ジョブ統一制御 API (2026-07-06)。

全背景ジョブ (K1 pipeline / K2 bespoke / K3 reactive) を 1 つの Job 抽象として
list / toggle / reschedule / trigger する。schedule/enabled は job_registry (DB SSoT)
に版保存され、稼働中スケジューラへ即反映される (再起動でも維持)。

write 系 (toggle/schedule/run) は READ_ONLY instance では middleware が 403 で block。
protection=critical の無効化はサーバ側でも confirm を要求する (ミス防止の二重防御)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.logging_config import get_logger
from src.scheduler.job_registry import (
    JobDef,
    apply_schedule_to_scheduler,
    chain_membership,
    danger_window_note,
    danger_windows,
    get_job,
    load_jobs,
    set_job_enabled,
    update_job_schedule,
    validate_schedule,
)
from src.scheduler.job_running import JobBusyError, running_jobs

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


def _ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _latest(*records: dict[str, str] | None) -> dict[str, str] | None:
    """最終実行の記録のうち新しい方 (job_last_run と runs の両方に出るジョブがある)。

    従来は job_last_run を常に優先していたため、チェーンの段になった pipeline は
    runs 側の running が見えなかった (2026-09-26)。
    """
    present = [r for r in records if r]
    if not present:
        return None
    return max(present, key=lambda r: _ts(r.get("last_run_at")) or datetime.min.astimezone())


def _live_state(scheduler: Any, job_id: str) -> tuple[str | None, bool | None]:
    """(次回実行 ISO, 一時停止中か)。scheduler が無い/未登録なら (None, None)。"""
    if scheduler is None:
        return None, None
    try:
        nr = scheduler.next_run_at(job_id)
        return (nr.isoformat() if nr else None), scheduler.is_paused(job_id)
    except Exception:  # noqa: BLE001 — job 未登録等は None
        return None, None


def _active_chains(jobs: list[JobDef]) -> dict[str, JobDef]:
    """段 → 有効なチェーン。無効なチェーンの段は単独ジョブとして振る舞う (rollback 経路)。"""
    return {sid: c for sid, c in chain_membership(jobs).items() if c.enabled}


class _Ctx:
    """一覧 1 回分の共有状態 (記録・チェーン所属・実行中の台帳)。"""

    def __init__(
        self,
        *,
        scheduler: Any,
        last_bespoke: dict[str, dict[str, str]],
        last_pipeline: dict[str, dict[str, str]],
        jobs: list[JobDef],
    ) -> None:
        self.scheduler = scheduler
        self.jobs = jobs
        self.chain_of = _active_chains(jobs)
        self.memory = running_jobs()
        self.last = {j.id: _latest(last_bespoke.get(j.id), last_pipeline.get(j.id)) for j in jobs}

    def running_since(self, job_id: str) -> str | None:
        if job_id in self.memory:
            return self.memory[job_id].isoformat()
        rec = self.last.get(job_id)
        if rec and rec.get("status") == "running":
            return rec.get("last_run_at")
        return None


def _job_view(j: JobDef, ctx: _Ctx) -> dict[str, Any]:
    """JobDef + ライブ状態 (次回・停止・最終実行・実行中・チェーン所属) を 1 dict にまとめる。"""
    chain = ctx.chain_of.get(j.id)
    live_id = chain.id if chain is not None else j.id
    next_run, is_paused = (
        (None, None) if j.kind == "reactive" else _live_state(ctx.scheduler, live_id)
    )
    view: dict[str, Any] = {
        **j.model_dump(mode="json"),
        "schedule_label": j.schedule_label(),
        "next_run_at": next_run,
        "is_paused": is_paused,
        "last_run": ctx.last.get(j.id),
        "danger_note": danger_window_note(j, ctx.jobs),
        "running_since": ctx.running_since(j.id),
        "chain_id": None,
        "running_step": None,
    }
    if chain is not None:
        # 段: 時刻も ON/OFF もチェーンが持つ。単独ジョブとしての enabled=False は
        # 「単独発火しない」の意味で、停止ではない (停止中の重要ジョブに数えない)
        idx = chain.steps.index(j.id) + 1
        view.update(
            chain_id=chain.id,
            chain_title=chain.title,
            enabled=chain.enabled,
            schedule_label=f"{chain.title}の {idx}/{len(chain.steps)} 段目",
            danger_note=None,
        )
    if j.kind == "chain":
        view["running_step"] = next(
            (sid for sid in j.steps if ctx.running_since(sid) is not None), None
        )
    return view


@jobs_api.get("")
def list_jobs_endpoint(request: Request) -> dict[str, Any]:
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
    ctx = _Ctx(
        scheduler=scheduler, last_bespoke=last_bespoke, last_pipeline=last_pipeline, jobs=jobs
    )
    view = [_job_view(j, ctx) for j in jobs]
    disabled_important = [
        j.id
        for j in jobs
        if not j.enabled
        and j.protection in ("critical", "important")
        and j.id not in ctx.chain_of  # 有効なチェーンの段は停止ではない
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
def job_runs_endpoint(job_id: str, request: Request, limit: int = 20) -> dict[str, Any]:
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
def toggle_job(job_id: str, req: ToggleRequest, request: Request) -> dict[str, Any]:
    """ジョブの enabled を切替える (protection=critical の停止は confirm 必須)。"""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"ジョブが見つかりません: {job_id}")
    _reject_chain_step(job_id, "ON/OFF")
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
def reschedule_job(job_id: str, req: ScheduleRequest, request: Request) -> dict[str, Any]:
    """ジョブのスケジュールを変更する (検証 + 危険時刻警告、live 反映)。"""
    current = get_job(job_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"ジョブが見つかりません: {job_id}")
    _reject_chain_step(job_id, "時刻")
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


def _reject_chain_step(job_id: str, what: str) -> None:
    """有効なチェーンの段は単独で ON/OFF・時刻変更できない (二重実行・無効な設定になる)。"""
    chain = _active_chains(load_jobs()).get(job_id)
    if chain is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"この処理は「{chain.title}」の段として実行されています。{what}は"
                f"「{chain.title}」で設定してください。"
            ),
        )


def _run_conflict(job: JobDef) -> str | None:
    """手動実行がチェーンと重なるなら理由を返す (同じジョブ自身の実行中は trigger_now が判定)。"""
    jobs = load_jobs()
    running = running_jobs()
    chain = _active_chains(jobs).get(job.id)
    if chain is not None and chain.id in running:
        return f"「{chain.title}」が実行中です。この段はチェーンの中で順番に実行されます。"
    if job.kind == "chain":
        busy = [sid for sid in job.steps if sid in running]
        if busy:
            titles = {j.id: j.title for j in jobs}
            step = titles.get(busy[0], busy[0])
            return f"段「{step}」が単独で実行中です。終わってから実行してください。"
    return None


@jobs_api.post("/{job_id}/run")
def run_job_now(job_id: str, request: Request) -> dict[str, Any]:
    """ジョブを今すぐ 1 回実行する (定時の予定は変えない。reactive は不可)。"""
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
    conflict = _run_conflict(job)
    if conflict is not None:
        raise HTTPException(status_code=409, detail=conflict)
    try:
        run_at = scheduler.trigger_now(job_id)
    except JobBusyError as e:
        raise HTTPException(status_code=409, detail=f"「{job.title}」は実行中です。") from e
    except KeyError as e:
        raise HTTPException(
            status_code=409, detail=f"「{job.title}」はスケジューラに登録されていません。"
        ) from e
    except Exception as e:  # noqa: BLE001
        _log.warning("job_trigger_failed", job_id=job_id, error=str(e))
        raise HTTPException(status_code=500, detail="実行の開始に失敗しました") from e
    _log.info("job_triggered_manually", job_id=job_id)
    return {"ok": True, "job_id": job_id, "triggered_at": run_at.isoformat()}
