"""FastAPI アプリ (Phase 1.5)。

エントリポイント:
    uvicorn src.ui.app:app --host 127.0.0.1 --port 8000

CLAUDE.md §11/§12 に従う:
- 127.0.0.1 のみバインド (起動コマンドで強制、Docker compose の ports でも強制)
- 認証なし (単独 Mac / 単独ユーザ前提)
- 編集対象 allowlist は src/ui/services/file_editor.py で実装
- シークレットマスクは src/ui/services/env_editor.py で実装

設計:
- AppState (app.state) に依存サービス (config, repo, scheduler, runner) を保持
- ルータは app.state を読み取る
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import Receive, Scope, Send

from src.config_loader import load_app_config, load_pipelines
from src.logging_config import get_logger
from src.scheduler.scheduler import BriefingScheduler, ScheduledPipeline
from src.storage.run_history import RunHistoryRepository
from src.ui.read_only_policy import build_read_only_middleware, request_auth_state
from src.ui.routers import config as config_router
from src.ui.routers import dashboard as dashboard_router
from src.ui.routers import health as health_router
from src.ui.routers import history as history_router
from src.ui.routers import intel_graph as intel_graph_router
from src.ui.routers import prompts as prompts_router
from src.ui.routers import runs as runs_router
from src.ui.routers import schedule as schedule_router
from src.ui.routers import subscriptions as subscriptions_router
from src.ui.routers.auth import build_auth_router
from src.ui.services.cf_access import AccessVerifier, load_access_config
from src.ui.services.file_editor import FileEditor
from src.ui.services.pipeline_runner import PipelineRunner

_log = get_logger(__name__)

# templates / static の絶対パス
_PKG_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _PKG_DIR / "templates"
STATIC_DIR = _PKG_DIR / "static"

# Phase 5B: DB 肥大化対策の設定値
# 90 日: 個人運用での「同じインシデントの follow-up 記事」検出には十分長く、
# embedding 蓄積 (年 ~40MB) を抑える妥協点。`config/.env` で書き換える要件が
# 出てから可変化する (今は YAGNI)。
DEDUP_RETENTION_DAYS = 90
# 30 日: VACUUM はコスト高いので毎回は避ける。月 1 を sentinel ファイル mtime で
# 判定する。
VACUUM_INTERVAL_DAYS = 30


def _register_local_tz_filter(templates: Jinja2Templates) -> None:
    """``to_local`` フィルタを Jinja 環境に登録する。

    DB 由来の UTC datetime を設定された timezone に変換し、None / 既に
    naive なものは安全に通す。``{{ dt|to_local|strftime("%H:%M") }}`` で利用可。
    """
    from datetime import datetime as _datetime
    from zoneinfo import ZoneInfo

    try:
        cfg = load_app_config()
        tz_name = cfg.timezone or "Asia/Tokyo"
    except Exception:  # noqa: BLE001
        tz_name = "Asia/Tokyo"
    tz = ZoneInfo(tz_name)

    def to_local(dt: _datetime | None) -> _datetime | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            # SQLite から戻ってきた "+00:00" 付きでない値は UTC 扱い
            from datetime import UTC

            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(tz)

    templates.env.filters["to_local"] = to_local


def _maybe_vacuum(
    repo: RunHistoryRepository,
    sentinel: Path,
    interval_days: int = VACUUM_INTERVAL_DAYS,
) -> None:
    """``data/.last_vacuum`` の mtime を見て、N 日以上経過していれば VACUUM する。

    起動時 1 回のみ呼ぶこと。失敗しても起動は止めない (既存 DB が壊れている
    レアケースだけログに残す)。
    """
    from datetime import UTC, datetime

    should_run = True
    if sentinel.exists():
        last = datetime.fromtimestamp(sentinel.stat().st_mtime, tz=UTC)
        if (datetime.now(UTC) - last).days < interval_days:
            should_run = False
    if not should_run:
        return
    try:
        repo.vacuum()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        _log.info("db_vacuumed", interval_days=interval_days)
    except Exception as e:  # noqa: BLE001
        _log.warning("db_vacuum_failed", error=str(e))


def _resolve_project_root() -> Path:
    """プロジェクトルートを解決する。コンテナ内では /app、ホストでは git ルート。"""
    # 環境変数で上書き可能 (テスト用)
    import os

    if env_root := os.environ.get("CTI_PROJECT_ROOT"):
        return Path(env_root).resolve()
    # src/ui/app.py → src/ui → src → repo
    return _PKG_DIR.parent.parent


def _collect_scheduled_pipelines() -> list[ScheduledPipeline]:
    """config/pipelines.yaml から自動起動対象の pipeline を抜き出す。"""
    try:
        pipelines = load_pipelines()
    except Exception as e:  # noqa: BLE001
        _log.error("pipelines_yaml_load_failed", error=str(e))
        return []
    out: list[ScheduledPipeline] = []
    for p in pipelines:
        if p.schedule is None or not p.schedule.enabled:
            continue
        out.append(
            ScheduledPipeline(
                name=p.name,
                hour=p.schedule.hour,
                minute=p.schedule.minute,
                enabled=True,
                display_name=f"{p.name} pipeline",
                interval_minutes=p.schedule.interval_minutes,
                interval_offset_minutes=p.schedule.interval_offset_minutes,
                day_of_week=p.schedule.day_of_week,
                day=p.schedule.day,
            ),
        )
    return out


def _register_bespoke_jobs(scheduler: BriefingScheduler, repo: RunHistoryRepository) -> None:
    """job_registry の bespoke ジョブを callable に結び付け registry schedule で登録する。

    callable はコード所有 (user 定義不可 = 安全)。schedule/enabled は registry (DB) が持つ。
    各実行を job_last_run に記録し「いつ最後に走り成否は」を可視化する。enabled 反映
    (無効=pause) は start() 後の apply_job_registry が行う。
    """
    from src.scheduler.job_registry import load_jobs

    def _tracked(job_id: str, fn: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[None]]:
        async def _run() -> None:
            # 2026-07-06 堅牢化: 記録は finally で必ず行う (旧実装は record 前に
            # BaseException=CancelledError 等が抜けると job_last_run に何も残らず、
            # bespoke daily job の失敗が完全に不可視だった)。cancellation は伝播させる。
            status = "succeeded"
            detail = ""
            try:
                await fn()
            except BaseException as e:  # noqa: BLE001 — cancellation 含め記録してから伝播
                status = "failed"
                detail = f"{type(e).__name__}: {e}"
                _log.error("bespoke_job_failed", job_id=job_id, error=str(e))
                # 失敗を ops に可視化 (bespoke ジョブは runs 経路の失敗通知に載らないため、
                # ここで明示通知しないと silently 死ぬ)。テスト/READ_ONLY は seam が抑止。
                with contextlib.suppress(Exception):
                    from src.ui.services.ops_notify import post_ops_message

                    await post_ops_message(
                        title=f"🔴 {job_id} ジョブ失敗",
                        body=f"{job_id} 失敗 · {detail[:200]}",
                        importance="high",
                        analyst_note=detail[:300],
                    )
                raise
            finally:
                with contextlib.suppress(Exception):
                    repo.record_job_run(job_id, status=status, detail=detail)

        return _run

    async def _ransomware_ingest() -> None:
        from src.sources.ransomware_ingest import run_ingest

        await run_ingest()

    async def _pir_rebuild() -> None:
        from src.pir.llm_judge import judge_pending
        from src.pir.persist import rebuild_pir_entities

        # 概念 PIR の LLM 主題判定 (incremental)。失敗しても rebuild は続ける —
        # verdict が古い/欠けるだけで照合自体は決定論のまま成立する。
        try:
            _log.info("pir_llm_judged", **await judge_pending())
        except Exception as e:  # noqa: BLE001 — judge 障害で rebuild を殺さない
            _log.warning("pir_llm_judge_failed", error=str(e))
        _log.info("pir_persist_rebuilt", **rebuild_pir_entities())

    async def _pir_judge_hourly() -> None:
        from src.pir.llm_judge import judge_hourly

        _log.info("pir_llm_judged_hourly", **await judge_hourly())

    async def _ledger_deep_review() -> None:
        from src.assessment.deep_review import run_deep_review
        from src.config_loader import load_app_config
        from src.tools.model_tiers import (
            Step,
            build_llm_for,
            is_external_model,
            resolve_narrative_think,
        )

        cfg = load_app_config()
        # narrative ティア経由 = モデルも think 設定も UI の narrative カードに従う
        # (factory が auto+外部のとき ThinkOnClient を注入する。手動 wrap はしない)。
        llm = build_llm_for(Step.LEDGER_DEEP_REVIEW, cfg)
        # think 品質が目的のジョブ — think が効かない構成 (ローカル割当 or 設定 off)
        # では昼と同品質の再評価になるだけなので skip。
        if not is_external_model(llm.model) or resolve_narrative_think() != "auto":
            _log.info(
                "deep_review_skipped_no_think",
                model=llm.model,
                narrative_think=resolve_narrative_think(),
            )
            return
        # adversarial ゲートは昼と同一条件 (reasoning ティア・think なし) に揃える
        adversarial_llm = build_llm_for(Step.SYNTHESIS_ANALYSIS, cfg)
        _log.info(
            "deep_review_done_stats",
            **await run_deep_review(llm=llm, adversarial_llm=adversarial_llm),
        )

    from src.ui.services.actor_history_distill import run_actor_history_distill
    from src.ui.services.body_refetch_backlog import run_body_refetch_backlog
    from src.ui.services.body_translate_backlog import run_body_translate_backlog
    from src.ui.services.fill_rate_audit import run_weekly_fill_rate_audit
    from src.ui.services.maintenance import run_daily_maintenance
    from src.ui.services.source_health import run_daily_heartbeat
    from src.ui.services.ua_health import run_ua_health_check

    async def _job_recovery() -> None:
        from src.scheduler.job_recovery import run_job_recovery

        await run_job_recovery(scheduler, repo.db_path)

    callables: dict[str, Callable[[], Awaitable[Any]]] = {
        "ransomware-live-ingest": _ransomware_ingest,
        "pir-entity-rebuild": _pir_rebuild,
        "pir-judge-hourly": _pir_judge_hourly,
        "ledger-deep-review": _ledger_deep_review,
        "body-translate-backlog": run_body_translate_backlog,
        "body-refetch-backlog": run_body_refetch_backlog,
        "ua-health-check": run_ua_health_check,
        "daily-maintenance": run_daily_maintenance,
        "daily-heartbeat": run_daily_heartbeat,
        "weekly-fill-rate-audit": run_weekly_fill_rate_audit,
        "actor-history-distill": run_actor_history_distill,
        "job-recovery-watchdog": _job_recovery,
    }
    jobs_by_id = {j.id: j for j in load_jobs()}
    for jid, fn in callables.items():
        jd = jobs_by_id.get(jid)
        if jd is None:
            continue
        wrapped = _tracked(jid, fn)
        if jd.schedule_type == "interval" and jd.interval_minutes:
            scheduler.attach_periodic(
                jid,
                wrapped,
                interval_minutes=jd.interval_minutes,
                offset_minutes=jd.offset_minutes or 0,
            )
        elif jd.schedule_type == "cron" and jd.hour is not None:
            scheduler.attach_daily(
                jid,
                wrapped,
                hour=jd.hour,
                minute=jd.minute or 0,
                day_of_week=jd.day_of_week,
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """起動時に scheduler を立ち上げ、終了時に停止する。"""
    project_root = _resolve_project_root()
    repo = RunHistoryRepository(db_path=project_root / "data" / "run_history.db")

    # Phase 1.5b: 起動時に異常終了 run を failed に倒し、古いログを purge する
    recovered = repo.fail_dangling_runs()
    if recovered > 0:
        _log.warning("recovered_dangling_runs", count=recovered)
    purged = repo.purge_old_logs()
    if purged > 0:
        _log.info("purged_old_logs", count=purged)
    purged_job_runs = repo.purge_old_job_run_log()
    if purged_job_runs > 0:
        _log.info("purged_old_job_run_log", count=purged_job_runs)
    # Phase 0 F3: orphan article_entities (実在しない article 分) を掃除して集計 skew を防ぐ
    orphans = repo.cleanup_orphan_article_entities()
    if orphans > 0:
        _log.info("cleaned_orphan_entities", count=orphans)
    # Phase 1 K5: CISA KEV catalog を起動時に最新化 (TTL gated、fail-safe)。
    # pipeline subprocess は data/kev_cache.json を読むだけなので hot-path で fetch しない。
    try:
        from src.tools.kev_client import refresh_kev_catalog

        kev_n = refresh_kev_catalog()
        _log.info("kev_catalog_ready", count=kev_n)
    except Exception as e:  # noqa: BLE001
        _log.warning("kev_refresh_failed_at_startup", error=str(e))

    # Phase 2.5 K5b: NVD CVSS を bounded に warm (full instance のみ、deploy 時に少しずつ)。
    # routing/表示は data/nvd_cache.json を cache 読みするだけ。readonly では fetch しない。
    if not READ_ONLY_FLAG:
        try:
            from src.tools.nvd_client import refresh_cvss

            nvd_n = refresh_cvss(
                repo.recent_cve_values(days=14, limit=100),
                max_fetch=8,
                deadline_seconds=12.0,
            )
            if nvd_n:
                _log.info("nvd_cvss_warmed", fetched=nvd_n)
        except Exception as e:  # noqa: BLE001
            _log.warning("nvd_warm_failed_at_startup", error=str(e))

    # Phase 5B: DB 肥大化対策
    # - dedup_seen_urls + article_embeddings は重複排除 (URL ハッシュ + コサイン
    #   類似度) のための索引なので、再投稿防止に必要な期間だけ保持すればよい。
    #   embedding が DB サイズの支配的要因 (1024 dim float32 = ~4KB/行) のため、
    #   90 日より古いものは消す。
    purged_dedup = repo.purge_old_dedup_entries(days=DEDUP_RETENTION_DAYS)
    if purged_dedup > 0:
        _log.info("purged_old_dedup", count=purged_dedup)
    # - VACUUM: SQLite は DELETE 後に領域を返さないので、定期的な物理回収が必要。
    #   毎回はコストが高い (DB 全体を rewrite) ので、sentinel ファイルの mtime で
    #   30 日に 1 度に絞る。個人運用規模 (~100MB) なら数秒で完了する。
    _maybe_vacuum(repo, project_root / "data" / ".last_vacuum")

    runner = PipelineRunner(repo=repo)
    file_editor = FileEditor(project_root=project_root)

    # 運用 config の DB store seed (full instance のみ、未投入の key を yaml seed から取り込む)。
    # 以後 DB が正 (UI 編集は DB に版保存、git は触らない)。
    if not READ_ONLY_FLAG:
        try:
            from src.cti.ci_operator_roster import seed_operators_if_absent
            from src.cti.router import seed_source_quality_if_absent
            from src.cti.routing_rules import seed_routing_rules_if_absent
            from src.pir.integration import seed_pir_if_absent
            from src.scheduler.job_registry import seed_jobs_if_absent
            from src.sources.source_store import seed_all_if_absent as seed_sources_if_absent
            from src.tools.channel_registry import seed_channels_if_absent
            from src.tools.model_tiers import seed_model_tiers_if_absent
            from src.tools.product_routing import seed_product_routing_if_absent

            if seed_jobs_if_absent():
                _log.info("job_schedules_seeded_from_defaults")
            if seed_channels_if_absent():
                _log.info("channels_seeded_from_builtin")
            if seed_operators_if_absent():
                _log.info("jp_ci_operators_seeded_from_builtin")
            if seed_product_routing_if_absent():
                _log.info("product_routing_seeded_from_builtin")
            # モデルティア割当を BUILTIN 既定から初回 seed。以後 DB が正 (UI「モデル」タブで編集)。
            if seed_model_tiers_if_absent():
                _log.info("model_tiers_seeded_from_builtin")
            if seed_routing_rules_if_absent():
                _log.info("routing_rules_seeded_from_yaml")
            if seed_source_quality_if_absent():
                _log.info("source_quality_seeded_from_yaml")
            if seed_pir_if_absent():
                _log.info("pir_seeded_from_yaml")
            sources_seeded = seed_sources_if_absent()
            if any(sources_seeded.values()):
                _log.info("sources_seeded_from_yaml", seeded=sources_seeded)
        except Exception as e:  # noqa: BLE001
            _log.warning("operational_config_seed_at_startup_failed", error=str(e))

    async def run_named_pipeline(pipeline_name: str) -> None:
        """APScheduler から呼ばれるコールバック (pipeline_name 指定)。

        Phase 5A fix: 即時実行 UI と同じ subprocess 経路 (``start_subprocess_run``)
        を使う。これにより stdout が ``run_logs`` に逐次永続化され、ダッシュボード
        / 履歴の articles テーブルにも 1 ブリーフィング = 1 行が書き込まれる。
        以前は in-process の ``runner.run()`` を使っていたため、scheduler 経由の
        実行は run_logs / articles のいずれも空のままだった。
        """
        # 動的収集抑止 guard (2026-07-07): 収集ジョブ (rss/web-scraper) の発火が active な
        # heavy ジョブの run 区間に被る時だけ、その発火を止める。固定の夜間解析帯を廃し、
        # 実 heavy スケジュール + 想定処理時間から精密に判定 (隙間では収集は走る)。
        # 手動実行 (trigger-now) は本経路を通らないので影響しない。
        try:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo

            from src.scheduler.job_registry import is_collection_suppressed

            _now = _dt.now(ZoneInfo("Asia/Tokyo"))
            _dow = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[_now.weekday()]
            if is_collection_suppressed(
                pipeline_name,
                now_minute_jst=_now.hour * 60 + _now.minute,
                weekday=_dow,
                day_of_month=_now.day,
            ):
                _log.info("collection_suppressed_heavy_overlap", pipeline=pipeline_name)
                return
        except Exception as e:  # noqa: BLE001 — guard 障害で収集自体は止めない
            _log.warning("collection_guard_failed", pipeline=pipeline_name, error=str(e))
        try:
            await runner.start_subprocess_run(
                triggered_by="scheduler",
                dry_run=False,
                pipeline_name=pipeline_name,
            )
        except Exception as e:  # noqa: BLE001
            _log.error("scheduled_run_failed", pipeline=pipeline_name, error=str(e))

    # config/pipelines.yaml の pipelines[].schedule から自動起動 job を構築する
    # Phase Diamond verify-mobile fix: READ_ONLY=1 instance では scheduler を起動しない。
    # 両 instance で scheduler が走ると毎 cron で同 pipeline が 2 並行起動 → timeout。
    scheduler: BriefingScheduler | None = None
    if not READ_ONLY_FLAG:
        scheduled = _collect_scheduled_pipelines()
        if scheduled:
            scheduler = BriefingScheduler.from_pipelines(run_named_pipeline, scheduled)
        else:
            # 後方互換: schedule 設定が無ければ daily-briefing のみ既定で起動
            async def legacy_run() -> None:
                await run_named_pipeline("daily-briefing")

            scheduler = BriefingScheduler(legacy_run)

        # Phase 5R: 独立 watcher は config/pipelines.yaml の web_scraper source として
        # 主パイプラインに合流済 (project-zero / lac-watch pipeline)。

        # 統一ジョブ制御 (2026-07-06): bespoke ジョブ (K2) と reactive の schedule/enabled を
        # job_registry (DB SSoT) から読む。callable はコード所有 (user 定義不可 = 安全)、
        # schedule のみ registry (UI 編集可・再起動維持)。[[operational_config_db]] と同型。
        from src.scheduler.job_registry import apply_job_registry, load_jobs

        _register_bespoke_jobs(scheduler, repo)

        scheduler.start()

        # 起動時 registry overlay: pipeline の schedule override 反映 + 無効 job を pause。
        try:
            apply_job_registry(scheduler, load_jobs())
        except Exception as e:  # noqa: BLE001
            _log.warning("job_registry_apply_at_startup_failed", error=str(e))
    else:
        _log.info("scheduler_skipped_read_only_mode")

    app.state.project_root = project_root
    app.state.repo = repo
    app.state.runner = runner
    app.state.scheduler = scheduler
    app.state.file_editor = file_editor

    # 脅威アクター snapshot / 評価 cache の warmup (起動直後の初回表示 ~5s を吸収)。
    # daemon thread で非同期に走らせ、失敗しても起動は続行する (次のリクエストが
    # 通常経路で計算する)。tests (TestClient 起動) では dev DB を触らないよう skip。
    warmup_enabled = os.environ.get("SNAPSHOT_WARMUP", "1").strip() not in ("0", "false", "off")
    if warmup_enabled and not os.environ.get("PYTEST_CURRENT_TEST"):

        def _warmup_threat_caches() -> None:
            try:
                from src.ui.services.actor_threat import fetch_mission_threat_assessments
                from src.ui.services.threat_operations import fetch_threat_operations_snapshot

                fetch_threat_operations_snapshot(lookback_days=30)
                fetch_mission_threat_assessments()
                _log.info("threat_snapshot_warmup_done")
            except Exception as e:  # noqa: BLE001
                _log.warning("threat_snapshot_warmup_failed", error=str(e))

        threading.Thread(
            target=_warmup_threat_caches, name="threat-snapshot-warmup", daemon=True
        ).start()

    _log.info("ui_app_started", project_root=str(project_root))
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown()
        await runner.shutdown()
        _log.info("ui_app_shutdown")


READ_ONLY_FLAG = os.environ.get("READ_ONLY", "0").strip() in ("1", "true", "yes", "on")


def _js_bool(value: bool) -> str:
    """SPA seed 用の JS リテラル (index.html への埋め込みに使う)。"""
    return "true" if value else "false"


class _SelectiveGZipMiddleware(GZipMiddleware):
    """SSE (text/event-stream) を除外した GZip 圧縮。

    gzip は内部バッファを持つため SSE (live log の /stream) はイベント到達が遅延しうる。
    /stream で終わるパスのみ素通しし、他は圧縮する (daily-briefs 等の大きな JSON が
    トンネル越しで数 MB → 数百 KB になる。2026-07-31 ブリーフ表示遅延対応)。
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and str(scope.get("path", "")).endswith("/stream"):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Kuebiko",
        description="久延毘古 — CTI ブリーフィングを生成・配信する個人運用パイプライン",
        version="0.2.0",
        docs_url=None,  # /docs を露出しない (内部 API は管理画面経由)
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    # Phase Diamond verify-mobile: READ_ONLY=1 のとき write 全 block。
    # 2026-08-01: Cloudflare Access が設定済みなら Tier1 (認証済み) 層を有効化し、
    # 運用系 read API の閲覧とジョブ即時実行だけを認証済み利用者に解放する。
    access_config = load_access_config()
    if READ_ONLY_FLAG:
        verifier = AccessVerifier(access_config) if access_config else None
        app.middleware("http")(build_read_only_middleware(verifier))
        _log.info("read_only_mode_enabled", auth_available=verifier is not None)
    app.include_router(build_auth_router(access_config))

    # 応答圧縮 (SSE を除く)。トンネル越し (kuebiko.example) の大きな JSON 転送を数分の一にする。
    app.add_middleware(_SelectiveGZipMiddleware, minimum_size=1024)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Phase 3.1 fix: テンプレート内の strftime が DB 由来の UTC 時刻を
    # そのまま表示してしまっていたため、設定された timezone (既定 Asia/Tokyo)
    # に変換する Jinja フィルタを登録する。``{{ dt|to_local }}`` で使う。
    _register_local_tz_filter(templates)

    app.state.templates = templates

    # コンテナ liveness probe (docker-compose healthcheck が叩く軽量エンドポイント)。
    # readiness (/api/v1/health-status = Ollama/Discord/IMAP の live プローブ) とは分離し、
    # ここは「プロセスが応答するか」だけを即時 200 で返す (外部 I/O なし)。重い readiness を
    # healthcheck に使うと flaky 化し、depends_on:service_healthy の起動を不当にブロックする。
    @app.get("/api/health")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(dashboard_router.router)
    app.include_router(history_router.router)
    app.include_router(runs_router.router)
    app.include_router(prompts_router.router)
    app.include_router(config_router.router)
    app.include_router(schedule_router.router)
    app.include_router(subscriptions_router.router)
    app.include_router(intel_graph_router.router)
    app.include_router(health_router.router)

    # Phase React: JSON API (v1) + WebSocket for SPA
    # Phase F: 旧 feeds/watchers/scrapers router 削除、 sources_api に統一
    from src.ui.api.actor_history import actor_history_api
    from src.ui.api.article_ops import article_ops_api
    from src.ui.api.articles_feed import articles_feed_api
    from src.ui.api.assistant import assistant_api
    from src.ui.api.channels import channels_api
    from src.ui.api.config_history import config_history_api
    from src.ui.api.dashboard_layout import dashboard_layout_api
    from src.ui.api.flow import flow_api
    from src.ui.api.geo import geo_api
    from src.ui.api.grok_mail import grok_mail_api
    from src.ui.api.grok_session import grok_session_api
    from src.ui.api.grok_tasks import grok_tasks_api
    from src.ui.api.jobs import jobs_api
    from src.ui.api.jp_ci_board import jp_ci_board_api
    from src.ui.api.jp_ci_operators import jp_ci_operators_api
    from src.ui.api.match_lists import match_lists_api
    from src.ui.api.model_tiers import model_tiers_api
    from src.ui.api.notes import notes_api
    from src.ui.api.pages import pages_api
    from src.ui.api.pir import pir_api
    from src.ui.api.product_routing import product_routing_api
    from src.ui.api.routing_rules import routing_rules_api
    from src.ui.api.runs import dash_api, runs_api
    from src.ui.api.situations import situations_api
    from src.ui.api.sources import sources_api
    from src.ui.api.spotlight import spotlight_api
    from src.ui.api.v1 import api as api_v1
    from src.ui.api.vocabularies import vocabularies_api
    from src.ui.ws.realtime import ws_router

    app.include_router(api_v1)
    app.include_router(assistant_api)
    app.include_router(runs_api)
    app.include_router(dash_api)
    app.include_router(pages_api)
    app.include_router(pir_api)
    app.include_router(jobs_api)
    app.include_router(routing_rules_api)
    app.include_router(channels_api)
    app.include_router(grok_mail_api)
    app.include_router(grok_session_api)
    app.include_router(grok_tasks_api)
    app.include_router(product_routing_api)
    app.include_router(model_tiers_api)
    app.include_router(jp_ci_board_api)
    app.include_router(jp_ci_operators_api)
    app.include_router(config_history_api)
    app.include_router(match_lists_api)
    app.include_router(flow_api)
    app.include_router(geo_api)
    app.include_router(spotlight_api)
    app.include_router(situations_api)
    app.include_router(actor_history_api)
    app.include_router(sources_api)
    app.include_router(dashboard_layout_api)
    # article_ops は articles_feed より先に登録する:
    # articles_feed の GET /articles/{article_id:path} が貪欲 match するため、
    # 後に登録すると /articles/{id}/stix が detail 扱いで 404 になる。
    app.include_router(article_ops_api)
    app.include_router(articles_feed_api)
    app.include_router(notes_api)
    app.include_router(vocabularies_api)
    app.include_router(ws_router)

    # React SPA built artifact (frontend/dist) を /app 配下で serve。
    # client-side routing (pathname-based) のため、/app/* の任意 path で
    # index.html を返す fallback を追加 (deep link / refresh 対応)。
    frontend_dist = Path("frontend/dist")
    if frontend_dist.exists():
        # static assets (/app/assets/*) は StaticFiles で正確に
        app.mount(
            "/app/assets",
            StaticFiles(directory=str(frontend_dist / "assets")),
            name="react-assets",
        )
        # その他の /app/* path はすべて React SPA index.html を返す
        index_path = frontend_dist / "index.html"

        @app.get("/app")
        @app.get("/app/")
        @app.get("/app/{path:path}")
        async def react_spa_fallback(request: Request, path: str = "") -> HTMLResponse:
            _ = path
            # index.html は必ず再検証させる (2026-07-29): Cache-Control 無しだとブラウザが
            # ヒューリスティックキャッシュで古い index.html を保持し、デプロイで bundle 名が
            # 変わっても古い (消えた) /app/assets/*.js を参照して 404 → 画面が開けなくなる。
            # ハッシュ付き assets は StaticFiles が長期キャッシュ可、index.html だけ no-cache。
            #
            # runtime flag を初回ペイント前に seed する (2026-07-31 サイドバー flash 根治):
            # useRuntimeFlags の CSR fetch 完了前に誤ったメニューが一瞬描画されるのを、
            # bundle 実行前の inline script で防ぐ。認証状態も同じ理由で seed する
            # (2026-08-01)。デプロイで index.html を差し替えるため毎回読み直す。
            authenticated, auth_available = request_auth_state(request)
            seed = ";".join(
                [
                    f"window.__READ_ONLY__={_js_bool(READ_ONLY_FLAG)}",
                    f"window.__AUTHENTICATED__={_js_bool(authenticated)}",
                    f"window.__AUTH_AVAILABLE__={_js_bool(auth_available)}",
                ]
            )
            html = index_path.read_text(encoding="utf-8").replace(
                "<head>",
                f"<head><script>{seed};</script>",
                1,
            )
            return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.exception_handler(404)
    async def not_found(request: Request, exc: object) -> HTMLResponse:  # noqa: ARG001
        return HTMLResponse(
            "<h1>404</h1><p><a href='/'>ダッシュボードに戻る</a></p>",
            status_code=404,
        )

    return app


app = create_app()
