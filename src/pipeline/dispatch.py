"""実ツール組み立て + pipeline 種別ディスパッチ (src.main から分割)。"""

from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import jinja2

from src.config_loader import (
    PipelineConfig,
    load_app_config,
    load_channel_routing,
    load_llm_enrichment,
    load_pipelines,
    load_stix_attach_policy,
)
from src.grok.fetcher import GrokFetcher
from src.logging_config import get_logger
from src.pipeline.filters import _try_build_embedder
from src.pipeline.orchestrator import run_pipeline
from src.pipeline.publish import _build_publishers
from src.pipeline.result import PipelineRunResult
from src.pipeline.runners import (
    _run_daily_brief_default,
    _run_digest_default,
    _run_mitre_actor_sync_default,
    _run_pir_spotlight_default,
    _run_status_synthesis_default,
    _run_taxonomy_review_default,
)
from src.storage.run_history import RunHistoryRepository
from src.tools.content_extractor import ContentExtractor
from src.tools.imap_client import ImapClient
from src.tools.llm_client import (
    LLMClient,
)
from src.tools.model_tiers import Step, build_llm_for
from src.tools.source_router import build_source

_log = get_logger(__name__)

DEFAULT_TEMPLATE_PATH = Path("prompts/summarizer.j2")
DEFAULT_PIPELINE_NAME = "daily-briefing"

RUN_RESULTS_DIR = Path("data/run_results")


def _resolve_time_budget_seconds() -> float | None:
    """親 (PipelineRunner) が subprocess spawn 時に渡す wallclock 予算 (秒) を読む。

    未設定 / 不正値なら None (soft deadline 無効 = 従来挙動)。in-process 経路や
    テストでは通常未設定。
    """
    raw = os.environ.get("PIPELINE_TIME_BUDGET_SECONDS", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


async def run_default(
    *,
    dry_run: bool = False,
    max_articles: int | None = None,
    pipeline_name: str = DEFAULT_PIPELINE_NAME,
    skip_dedup: bool = False,
    run_id: int | None = None,
) -> PipelineRunResult:
    """``.env`` と ``config/*.yaml`` から実ツールを組み立てて実行する。"""
    config = load_app_config()
    pipelines = load_pipelines()
    pipeline = _find_pipeline(pipelines, pipeline_name)

    # Phase 5T-J: digest pipeline (weekly_recap) は DB から既蓄積 article を集約 →
    # LLM digest → Discord brief 投稿の最小フロー。triage / dedup / per-article
    # enrichment は通らない。digest_research は pir_daily_focus に置き換え済。
    if pipeline.source.type == "digest_weekly_recap":
        return await _run_digest_default(
            config=config,
            pipeline=pipeline,
            dry_run=dry_run,
            run_id=run_id,
        )

    # Phase H: weekly-taxonomy-review (LLM 提案生成、UI で承認)
    if pipeline.source.type == "taxonomy_review":
        return await _run_taxonomy_review_default(
            config=config,
            pipeline=pipeline,
            dry_run=dry_run,
            run_id=run_id,
        )

    # Actors Stage 4: MITRE ATT&CK → Actor 辞書の週次逐次同期
    if pipeline.source.type == "mitre_actor_sync":
        return await _run_mitre_actor_sync_default(
            config=config,
            dry_run=dry_run,
            run_id=run_id,
        )

    # Phase 3 (Synthesis): 状況総括の週次/月次生成
    if pipeline.source.type == "status_synthesis":
        return await _run_status_synthesis_default(
            config=config,
            pipeline=pipeline,
            dry_run=dry_run,
            run_id=run_id,
        )

    # Phase Diamond verify-spotlight: PIR 縦断 narrative の週次生成
    if pipeline.source.type == "pir_spotlight":
        return await _run_pir_spotlight_default(
            config=config,
            pipeline=pipeline,
            dry_run=dry_run,
            run_id=run_id,
        )

    # 段4(a)/朝刊夕刊: 統合 #brief (morning=synthesis+PIR focus / evening=synthesis のみ)。
    # slot は pipeline 名で判定。type=morning_brief を朝夕共用 (新 SourceType 値を足さない)。
    if pipeline.source.type == "morning_brief":
        return await _run_daily_brief_default(
            config=config,
            pipeline=pipeline,
            dry_run=dry_run,
            run_id=run_id,
        )

    template = _load_template()
    channel_routing = load_channel_routing()
    enrichment = load_llm_enrichment()
    stix_policy = load_stix_attach_policy()

    # Phase 3a/3b: 重複排除リポジトリ (run_history と同じ DB を共有)
    dedup_repo = RunHistoryRepository()

    # Phase 3b: embedding クライアント (graceful degradation: 設定ない / モデル未取得なら None)
    embedder = _try_build_embedder(config)

    async with AsyncExitStack() as stack:
        extractor = await stack.enter_async_context(ContentExtractor())
        llm = build_llm_for(Step.ARTICLE_SUMMARY, config)
        # 旧 extract slot は fast ティアに統合済 (要約/抽出/triage は同一 fast モデル)。
        # triage は llm (fast) をそのまま使う (orchestrator が None を llm に吸収)。
        extract_llm: LLMClient | None = None

        # Phase 2: Grok email source の場合のみ IMAP / Playwright を立ち上げる
        imap_client: ImapClient | None = None
        grok_fetcher: GrokFetcher | None = None
        if pipeline.source.type == "grok_email":
            imap_client = await stack.enter_async_context(
                ImapClient(
                    host=config.imap_host,
                    port=config.imap_port,
                    user=config.imap_user,
                    password=config.imap_password,
                ),
            )
            grok_fetcher = await stack.enter_async_context(GrokFetcher())

        # P0 収集飢餓修正: seen 済み URL を source 層で除外してから max_count を競わせる
        # (rss のみ有効。skip_dedup 時は旧挙動 = 全 pool の published 降順 top-N)。
        source = build_source(
            pipeline.source,
            extractor=extractor,
            grok_fetcher=grok_fetcher,
            imap_client=imap_client,
            app_config=config,
            seen_hash_filter=None if skip_dedup else dedup_repo.filter_seen_and_touch,
        )

        # チャンネル別の DiscordPublisher を一度に作る
        publishers = _build_publishers(config)

        return await run_pipeline(
            config=config,
            pipeline=pipeline,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=dry_run,
            max_articles=max_articles,
            dedup_repo=dedup_repo,
            embedder=embedder,
            skip_dedup=skip_dedup,
            extract_llm=extract_llm,
            run_id=run_id,
            channel_routing=channel_routing,
            enrichment=enrichment,
            stix_policy=stix_policy,
            time_budget_seconds=_resolve_time_budget_seconds(),
        )

    # AsyncExitStack を抜けると ContextManager の出力がここに来る (型を満たすため)
    raise RuntimeError("unreachable")


def _find_pipeline(
    pipelines: list[PipelineConfig],
    name: str,
) -> PipelineConfig:
    for p in pipelines:
        if p.name == name:
            return p
    available = ", ".join(p.name for p in pipelines)
    raise ValueError(
        f"パイプライン '{name}' が config/pipelines.yaml に存在しません (利用可能: {available})",
    )


def _load_template(path: Path = DEFAULT_TEMPLATE_PATH) -> jinja2.Template:
    if not path.exists():
        raise FileNotFoundError(f"Jinja2 テンプレートが見つかりません: {path.resolve()}")
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(path.parent)),
        autoescape=False,  # noqa: S701  プロンプトはテキストでありエスケープしない
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
    return env.get_template(path.name)


def _write_run_result_file(run_id: int, result: PipelineRunResult) -> None:
    """subprocess 経路の親 (PipelineRunner) が finish_run の totals に使う JSON を書く。

    親プロセスはこのファイルを読み取って ``runs.total_fetched / summarized /
    posted / marked_read / error_count / note`` を埋める。失敗しても本処理は止めない。
    """
    try:
        RUN_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = RUN_RESULTS_DIR / f"{run_id}.json"
        payload = {
            "run_id": run_id,
            "total_fetched": result.total_fetched,
            "skipped_dup": result.skipped_dup,
            "summarized": result.summarized,
            "posted": result.posted,
            "marked_read": result.marked_read,
            "errors": result.errors,
            "dry_run": result.dry_run,
            # 監査 2026-07-05 P2: subprocess 経路で triage fail-open / partial fetch の
            # 可観測性が常に 0 で永続化されていた欠落の修正
            "triage_error_count": result.triage_error_count,
            "partial_fetch": result.partial_fetch,
            "partial_fetch_count": result.partial_fetch_count,
            # 監査 backlog 2026-07-05: 親の partial_failure/failed 通知の二重投稿判定
            "ops_notified": result.ops_notified,
            # 2026-08-01: soft deadline で次 run へ繰り越した件数 (親が note に反映)
            "deferred_count": result.deferred_count,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[warn] run_result write failed: {e}\n")
