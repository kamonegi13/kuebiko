"""digest pipeline runner (Phase 5T-J)。

F1 (weekly_recap) の実行をオーケストレーション (旧 E1 digest_research は pir_daily_focus 化)。
DB 取得 → LLM 集約 → Discord brief channel 投稿の最小フロー。
既存 run_pipeline の重い処理 (triage / dedup / per-article enrichment) は
通らない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from src.config_loader import AppConfig, DiscordChannel, PipelineConfig
from src.digest.db_filter import (
    fetch_for_deep_dive_candidates,
    fetch_recent_brief_titles,
)
from src.digest.deep_dive_selector import (
    ScoredArticle,
    select_deep_dive_articles,
)
from src.digest.llm_digest import generate_digest

# Phase G1 廃止: trend_aggregator は UI (V1/V3) から再利用、runner からの import 不要
from src.logging_config import get_logger
from src.storage.run_history import F1SelectionRecord, RunHistoryRepository
from src.tools.channel_registry import push_map
from src.tools.discord_publisher import (
    BriefingMessage,
    DiscordPublisher,
    Source,
)
from src.tools.llm_client import LLMClient
from src.tools.product_routing import product_channel

_log = get_logger(__name__)

# digest source.type → (LLM template, fetch fn, title prefix, default importance)
# digest_research は pir_daily_focus に置き換わったため削除済 (Phase Diamond pir-daily-focus)。
_DIGEST_SPECS = {
    "digest_weekly_recap": {
        "template": "weekly_recap.j2",
        "title_prefix": "週次深掘りダイジェスト",
        "category": "weekly_recap",
        "importance": "medium",
    },
    # Phase G1 trend digest は Phase H で廃止 (UI V1/V3 で代替)
    # aggregate_trends() ロジックは src/digest/trend_aggregator.py に保持、UI から再利用。
}


@dataclass(frozen=True)
class DigestRunResult:
    """digest pipeline 実行結果。"""

    candidates_count: int  # DB から取得した digest 対象件数
    posted: bool  # 実際に Discord に投稿したか (候補 0 件で skip 可能)
    digest_chars: int  # 生成された digest 本文の文字数
    errors: list[str]


def _week_label(lookback_hours: int = 168, tz_name: str = "Asia/Tokyo") -> str:
    """F1 期間ラベル (例: '2026-05-11 - 2026-05-17')。"""
    end = datetime.now(ZoneInfo(tz_name))
    from datetime import timedelta

    start = end - timedelta(hours=lookback_hours)
    return f"{start.strftime('%Y-%m-%d')} - {end.strftime('%Y-%m-%d')}"


# Phase 5T-T2: novelty 判定窓 (過去 4 週)
_F1_NOVELTY_LOOKBACK_HOURS = 672

# Phase G1 trend は Phase H で UI 化、baseline 窓は trend_aggregator のデフォルト引数で管理


async def _run_weekly_deep_dive(
    *,
    llm: LLMClient,
    repo: RunHistoryRepository | None,
    run_id: int | None,
    lookback_hours: int,
    dry_run: bool,
) -> list[ScoredArticle]:
    """Phase 5T-T2: weekly deep dive 選定。

    Stage 0+1 機械 prefilter → LLM rubric scoring → composite で 0-5 件。
    repo + run_id が揃っていれば f1_selections に記録。
    """
    past_selected_keys: set[str] = set()
    if repo is not None:
        past_selected_keys = repo.find_recent_f1_dedup_keys(
            lookback_hours=_F1_NOVELTY_LOOKBACK_HOURS,
        )

    prefilter = fetch_for_deep_dive_candidates(
        lookback_hours=lookback_hours,
        novelty_excluded_dedup_keys=past_selected_keys or None,
    )
    _log.info(
        "deep_dive_prefilter_done",
        stage_counts=prefilter.stage_counts,
        past_selected_count=len(past_selected_keys),
    )
    if not prefilter.candidates:
        _log.info("deep_dive_no_candidates")
        return []

    recent_briefs: list[str] = []
    if repo is not None:
        recent_briefs = fetch_recent_brief_titles(lookback_hours=lookback_hours)

    selected = await select_deep_dive_articles(
        llm=llm,
        candidates=prefilter.candidates,
        recent_briefs=recent_briefs,
        past_selected_keys=sorted(past_selected_keys),
    )

    if selected and repo is not None and run_id is not None and not dry_run:
        records = [
            F1SelectionRecord(
                run_id=run_id,
                article_id=s.candidate.article_id,
                dedup_key=s.candidate.dedup_key,
                composite_score=s.composite,
                pir=s.pir,
                roi=s.roi,
                timeliness=s.timeliness,
                novelty=s.novelty,
            )
            for s in selected
        ]
        inserted = repo.record_f1_selections(records)
        _log.info("deep_dive_selections_recorded", inserted=inserted)

    return selected


async def run_digest_pipeline(
    *,
    config: AppConfig,
    pipeline: PipelineConfig,
    llm: LLMClient,
    publishers: dict[DiscordChannel, DiscordPublisher],
    dry_run: bool = False,
    repo: RunHistoryRepository | None = None,
    run_id: int | None = None,
) -> DigestRunResult:
    """digest pipeline (E1 / F1) を実行する。

    Args:
        config: AppConfig
        pipeline: source.type が digest_research / digest_weekly_recap
        llm: LLM クライアント
        publishers: channel 別 DiscordPublisher (brief が必要)
        dry_run: True なら投稿せずログのみ
        repo: F1 deep dive で novelty 判定 + 履歴記録に使う (Phase 5T-T2)
        run_id: F1 selection 記録用 (None なら DB 記録 skip)
    """
    _ = config  # 現状未使用 (将来の dry_run 通知先切替で使用予定)
    source_type = pipeline.source.type
    spec = _DIGEST_SPECS.get(source_type)
    if spec is None:
        raise ValueError(f"unknown digest source type: {source_type}")

    lookback = pipeline.source.digest_lookback_hours
    # weekly recap は deep dive selector が件数を決める (digest_max_items は log 表示のみ)。
    # 段5 修正: 旧コードは未定義 max_items を L189 参照で candidates>0 時に NameError クラッシュ
    # していた (= recap 機能不全)。値を束縛してログに使う。
    max_items = pipeline.source.digest_max_items

    # weekly recap (F1): Phase 5T-T2 deep dive selector で 3-5 件選定
    deep_dive_result = await _run_weekly_deep_dive(
        llm=llm,
        repo=repo,
        run_id=run_id,
        lookback_hours=lookback,
        dry_run=dry_run,
    )
    candidates = [s.candidate for s in deep_dive_result]
    period_label = _week_label(lookback_hours=lookback)

    _log.info(
        "digest_pipeline_candidates",
        pipeline=pipeline.name,
        source_type=source_type,
        lookback_hours=lookback,
        max_items=max_items,
        candidates=len(candidates),
    )

    if not candidates:
        _log.info(
            "digest_pipeline_skip_empty",
            pipeline=pipeline.name,
        )
        return DigestRunResult(
            candidates_count=0,
            posted=False,
            digest_chars=0,
            errors=[],
        )

    # 2. LLM で digest 生成 (thinking mode OFF: digest はテキスト直行で十分、
    # Gemma 4 26B の thinking ブロックで本文が空になる症状を防ぐ)
    try:
        digest_text = await generate_digest(
            llm=llm,
            candidates=candidates,
            template_name=str(spec["template"]),
            period_label=period_label,
            think=False,
        )
    except Exception as e:  # noqa: BLE001
        _log.error("digest_llm_failed", pipeline=pipeline.name, error=str(e))
        return DigestRunResult(
            candidates_count=len(candidates),
            posted=False,
            digest_chars=0,
            errors=[f"llm_generate: {type(e).__name__}: {e}"],
        )

    if not digest_text.strip():
        _log.warning("digest_llm_empty_output", pipeline=pipeline.name)
        return DigestRunResult(
            candidates_count=len(candidates),
            posted=False,
            digest_chars=0,
            errors=["llm_empty_output"],
        )

    # 段5: recap 本文を永続化 (Retrospect で「あの週の深掘り」を後から読める)。投稿前に保存し
    # Discord 失敗でも本文を残す。dry-run / repo 不在では skip。
    if not dry_run and repo is not None:
        try:
            repo.record_weekly_recap(
                run_id=run_id,
                period_label=period_label,
                recap_text=digest_text,
                candidate_count=len(candidates),
            )
        except Exception as e:  # noqa: BLE001 — 永続化失敗で配信を止めない
            _log.error("weekly_recap_persist_failed", pipeline=pipeline.name, error=str(e))

    # 3. BriefingMessage を組み立てて Discord に投稿 (配信先は product_routing、情報フロー編集可)
    channel = product_channel("weekly_recap")
    title = f"{spec['title_prefix']} ({period_label})"
    message = BriefingMessage(
        title=title,
        bluf=f"watch ch 蓄積 {len(candidates)} 件から digest 生成",
        importance=str(spec["importance"]),  # type: ignore[arg-type]
        category=str(spec["category"]),
        summary=digest_text,
        sources=[
            Source(
                title=c.feed_title or "source",
                url=c.url,
                language="auto",
            )
            for c in candidates[:5]  # multi-embed 上限緩和、参考レベル
        ],
        metadata={
            "target_channel": channel,
            "digest_kind": source_type,
            "digest_candidates": len(candidates),
            "routing_reason": f"digest_pipeline:{source_type}",
            "routing_rule_id": "DIGEST",
        },
    )

    if dry_run:
        _log.info(
            "digest_pipeline_dry_run",
            pipeline=pipeline.name,
            title=title,
            digest_chars=len(digest_text),
        )
        return DigestRunResult(
            candidates_count=len(candidates),
            posted=False,
            digest_chars=len(digest_text),
            errors=[],
        )

    # 通知再設計: push=False の channel なら永続化のみで Discord 配信スキップ (Retrospect で閲覧)
    if not push_map().get(channel, True):
        _log.info("digest_web_only", pipeline=pipeline.name, channel=channel)
        return DigestRunResult(
            candidates_count=len(candidates),
            posted=False,
            digest_chars=len(digest_text),
            errors=[],
        )
    publisher = publishers.get(channel)
    if publisher is None:
        _log.error("digest_publisher_missing", pipeline=pipeline.name, channel=channel)
        return DigestRunResult(
            candidates_count=len(candidates),
            posted=False,
            digest_chars=len(digest_text),
            errors=[f"publisher_missing:{channel}"],
        )

    try:
        await publisher.post(message)
    except Exception as e:  # noqa: BLE001
        _log.error("digest_post_failed", pipeline=pipeline.name, error=str(e))
        return DigestRunResult(
            candidates_count=len(candidates),
            posted=False,
            digest_chars=len(digest_text),
            errors=[f"discord_post: {type(e).__name__}: {e}"],
        )

    _log.info(
        "digest_pipeline_posted",
        pipeline=pipeline.name,
        title=title,
        digest_chars=len(digest_text),
        candidates=len(candidates),
    )
    return DigestRunResult(
        candidates_count=len(candidates),
        posted=True,
        digest_chars=len(digest_text),
        errors=[],
    )
