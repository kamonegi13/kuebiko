"""Runs / Dashboard React SPA 用 JSON API。

旧 Jinja `/runs`, `/runs/{id}`, `/` を置き換える形で React が consume する。
SSE (`/runs/{id}/stream`) は既存 Jinja router の方をそのまま再利用する
(EventSource で React 側が消費可)。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request

from src.config_loader import load_pipelines
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.ui.services.pipeline_runner import PipelineBusyError, PipelineRunner

_log = get_logger(__name__)

runs_api = APIRouter(prefix="/api/v1/runs", tags=["runs"])
dash_api = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


def _run_to_dict(run: object) -> dict[str, Any]:
    from src.storage.run_history import RunRecord

    if not isinstance(run, RunRecord):
        return {}
    return {
        "id": run.id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "pipeline": run.pipeline,
        "dry_run": run.dry_run,
        "triggered_by": run.triggered_by,
        "status": run.status,
        "total_fetched": run.total_fetched,
        "summarized": run.summarized,
        "posted": run.posted,
        "marked_read": run.marked_read,
        "error_count": run.error_count,
        "note": run.note,
        "log_line_count": run.log_line_count,
        "log_truncated": bool(run.log_truncated),
    }


# ---------- /api/v1/runs ----------


@runs_api.get("/pipelines")
async def list_pipelines() -> dict[str, Any]:
    """pipelines.yaml に定義された実行可能 pipeline 一覧。"""
    try:
        pipelines = load_pipelines()
        opts = [{"name": p.name, "source_type": p.source.type} for p in pipelines]
    except Exception as e:  # noqa: BLE001
        _log.warning("pipelines_yaml_load_failed_for_api", error=str(e))
        opts = [{"name": "direct-rss-fetch", "source_type": "rss"}]
    return {"pipelines": opts}


@runs_api.get("/recent")
async def recent_runs(request: Request, limit: int = 10) -> dict[str, Any]:
    """直近 run 一覧 + 現在 running の run。"""
    repo: RunHistoryRepository = request.app.state.repo
    running = repo.list_runs(limit=1, status="running")
    recent = repo.list_runs(limit=max(1, min(limit, 50)))
    return {
        "running": [_run_to_dict(r) for r in running],
        "recent": [_run_to_dict(r) for r in recent],
    }


@runs_api.post("/start")
async def start_run(
    request: Request,
    dry_run: str | None = Form(default=None),
    max_articles: int | None = Form(default=None),
    pipeline_name: str = Form(default="direct-rss-fetch"),
    skip_dedup: str | None = Form(default=None),
) -> dict[str, Any]:
    """新規 pipeline run を subprocess で起動、run_id を返す。"""
    runner: PipelineRunner = request.app.state.runner
    is_dry = dry_run == "1"
    try:
        valid_names = {p.name for p in load_pipelines()}
    except Exception:  # noqa: BLE001
        valid_names = {"direct-rss-fetch"}
    if pipeline_name not in valid_names:
        candidates = "、".join(sorted(valid_names))
        raise HTTPException(
            status_code=400,
            detail=f"未知の処理です: {pipeline_name} (指定可能: {candidates})",
        )
    is_skip_dedup = skip_dedup == "1"
    try:
        run_id = await runner.start_subprocess_run(
            dry_run=is_dry,
            max_articles=max_articles,
            pipeline_name=pipeline_name,
            skip_dedup=is_skip_dedup,
            triggered_by="manual",
        )
    except PipelineBusyError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"run_id": run_id}


@runs_api.get("/{run_id}")
async def get_run(request: Request, run_id: int) -> dict[str, Any]:
    """1 run の status + meta。"""
    repo: RunHistoryRepository = request.app.state.repo
    run = repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"実行 {run_id} が見つかりません")
    return _run_to_dict(run)


@runs_api.get("/{run_id}/log")
async def get_run_log(
    request: Request,
    run_id: int,
    from_seq: int = 0,
) -> dict[str, Any]:
    """既存 log 行を一括取得 (SSE 接続前の initial state 用)。"""
    repo: RunHistoryRepository = request.app.state.repo
    run = repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"実行 {run_id} が見つかりません")
    lines = repo.get_log_lines(run_id, from_seq=from_seq)
    return {
        "run_id": run_id,
        "status": run.status,
        "lines": [
            {"seq": ln.seq, "ts": ln.ts.isoformat(), "stream": ln.stream, "line": ln.line}
            for ln in lines
        ],
    }


# ---------- /api/v1/dashboard ----------


@dash_api.get("/summary")
async def dashboard_summary(request: Request, days: int = 7) -> dict[str, Any]:
    """ダッシュボード集計 (直近 N 日)。"""
    days = max(1, min(days, 90))
    repo: RunHistoryRepository = request.app.state.repo
    daily = repo.daily_post_counts(days=days)
    # importance 分布は **サイバー脅威カテゴリのみ** (geopolitical 等の戦略 importance を
    # 混ぜず脅威トリアージの分布を表す。dashboard の importance_mix widget が消費)。
    importance = repo.importance_breakdown(days=days, cyber_only=True)
    failure_rate = repo.extract_failure_rate(days=days)
    # 切り株率 (2026-07-27): 全文取得失敗→feed 抜粋 fallback の割合 (無音失敗の可視化)。
    stump_rate = repo.stump_body_rate(days=days)
    recent = repo.list_runs(limit=10)
    # readonly instance では scheduler が None (Phase Diamond verify-mobile 設計)
    scheduler = request.app.state.scheduler
    next_run = scheduler.next_run_at() if scheduler is not None else None
    db_stats = repo.db_stats()
    return {
        "days": days,
        "summary": {
            "total_runs": len(recent),
            "posted_total": sum(c for _, c in daily),
            "extract_failure_rate": failure_rate,
            "stump_body_rate": stump_rate,
            "importance": importance,
            "daily_posts": [list(p) for p in daily],
        },
        "recent_runs": [_run_to_dict(r) for r in recent],
        "next_run_at": next_run.isoformat() if next_run else None,
        "db_stats": db_stats,
    }
