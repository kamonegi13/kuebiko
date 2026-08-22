"""特殊 pipeline (digest / synthesis / spotlight / daily-brief 等) の runner。

src.main から分割。
"""

from __future__ import annotations

from typing import Any

from src.config_loader import AppConfig, PipelineConfig
from src.logging_config import get_logger
from src.pipeline.publish import _build_publishers
from src.pipeline.result import PipelineRunResult
from src.storage.run_history import RunHistoryRepository
from src.tools.channel_registry import push_map
from src.tools.discord_publisher import BriefingMessage, Source
from src.tools.model_tiers import Step, build_llm_for
from src.tools.product_routing import product_channel

_log = get_logger(__name__)


# ---------- Phase 5T-J: digest pipeline 専用 runner ----------


async def _run_digest_default(
    *,
    config: AppConfig,
    pipeline: PipelineConfig,
    dry_run: bool,
    run_id: int | None = None,
) -> PipelineRunResult:
    """digest pipeline (E1 / F1) を実行し PipelineRunResult を返す。

    既存 run_pipeline と違い fetch/triage/dedup/enrichment を持たないため、
    LLM + brief publisher のみ立ち上げる。
    F1 (weekly deep dive) は run_id + RunHistoryRepository を渡して
    f1_selections を永続化する (Phase 5T-T2)。
    """
    from src.digest.runner import run_digest_pipeline

    # Phase 5T-Z fix: F1 weekly-recap deep dive は rubric scoring + 12k max_tokens
    # narrative で 1 回の LLM call が 5-10 分超になる。fast ティア (26B) + 900s timeout。
    llm = build_llm_for(Step.DIGEST_DEEP_DIVE, config)
    publishers = _build_publishers(config)

    # Phase 5T-T2: F1 deep dive で novelty 判定 + 履歴記録のため共有
    repo = RunHistoryRepository()

    result = await run_digest_pipeline(
        config=config,
        pipeline=pipeline,
        llm=llm,
        publishers=publishers,
        dry_run=dry_run,
        repo=repo,
        run_id=run_id,
    )

    # PipelineRunResult に digest 結果をマップ
    return PipelineRunResult(
        total_fetched=result.candidates_count,
        summarized=1 if result.posted else 0,
        posted=1 if result.posted else 0,
        marked_read=0,
        errors=result.errors,
    )


# ---------- Phase 3: status synthesis pipeline runner ----------


async def _deliver_synthesis_discord(
    config: AppConfig,
    repo: RunHistoryRepository,
    generated_map: dict[str, bool],
) -> None:
    """Phase 2 K6: 生成済 synthesis を配信 (未配信のみ)。失敗は飲み込む。

    配信先は product_routing (情報フロー編集可)。push=False の channel なら DB 永続のみで
    Discord 配信はスキップ (Synthesis タブで pull 閲覧)。
    """
    generated = [p for p, ok in generated_map.items() if ok]
    if not generated:
        return
    channel = product_channel("status_synthesis")
    if not push_map().get(channel, True):
        _log.info("synthesis_web_only", channel=channel, periods=generated)
        return
    publisher = _build_publishers(config).get(channel)
    if publisher is None:
        _log.warning("synthesis_discord_skipped", reason=f"no publisher for {channel}")
        return
    try:
        from src.synthesis.discord import deliver_synthesis

        delivered = await deliver_synthesis(repo=repo, publisher=publisher, period_types=generated)
        if delivered:
            _log.info("synthesis_discord_delivered", periods=delivered, channel=channel)
    except Exception as e:  # noqa: BLE001
        _log.error("synthesis_discord_delivery_failed", error=str(e))


async def _deliver_spotlight_discord(
    config: AppConfig,
    repo: RunHistoryRepository,
    pir_ids: list[str],
    period_type: str,
) -> None:
    """Phase 2 K6: 生成済 spotlight を配信 (未配信のみ)。失敗は飲み込む。

    配信先は product_routing (情報フロー編集可)。push=False の channel なら DB 永続のみで
    Discord 配信はスキップ (Synthesis タブの Spotlight で pull 閲覧)。
    """
    if not pir_ids:
        return
    channel = product_channel("pir_spotlight")
    if not push_map().get(channel, True):
        _log.info("spotlight_web_only", channel=channel, pir_ids=pir_ids)
        return
    publisher = _build_publishers(config).get(channel)
    if publisher is None:
        _log.warning("spotlight_discord_skipped", reason=f"no publisher for {channel}")
        return
    try:
        from src.spotlight.discord import deliver_spotlights

        delivered = await deliver_spotlights(
            repo=repo, publisher=publisher, pir_ids=pir_ids, period_type=period_type
        )
        if delivered:
            _log.info("spotlight_discord_delivered", pir_ids=delivered, channel=channel)
    except Exception as e:  # noqa: BLE001
        _log.error("spotlight_discord_delivery_failed", error=str(e))


async def _run_status_synthesis_default(
    *,
    config: AppConfig,
    pipeline: PipelineConfig,
    dry_run: bool,
    run_id: int | None = None,
) -> PipelineRunResult:
    """Phase 3: status-synthesis pipeline。

    PMESII-PT 軸別 article + trend cluster + PIR を LLM に渡し、
    軸間関係に焦点を当てた状況総括を生成、status_synthesis テーブルへ UPSERT。

    pipeline.source.synthesis_periods で対象期間を指定:
    - daily-status-synthesis: ("daily",) → 24h window、~2 回/日 cron で更新
    - weekly-status-synthesis: () または ("weekly", "monthly") → 既存
    """
    _ = run_id
    from src.synthesis.runner import run_status_synthesis

    # 散文は narrative ティア、台帳 ACH/射影は reasoning ティア (think ティア分離 2026-07-24)。
    # detect-new 等「入力が多い triage」は fast ティア (26B) — Dense 31B で 150+ 件を読むと
    # 900s timeout していた根治。
    llm = build_llm_for(Step.SYNTHESIS_NARRATIVE, config)
    analysis_llm = build_llm_for(Step.SYNTHESIS_ANALYSIS, config)
    fast_llm = build_llm_for(Step.SYNTHESIS_DETECT, config)
    _log.info(
        "synthesis_pipeline_start",
        pipeline=pipeline.name,
        model=llm.model,
        analysis_model=analysis_llm.model,
        fast_model=fast_llm.model,
        timeout_seconds=900.0,
    )
    repo: RunHistoryRepository | None = None if dry_run else RunHistoryRepository()
    periods = pipeline.source.synthesis_periods or None
    result = await run_status_synthesis(
        llm=llm,
        repo=repo,
        period_types=periods,
        fast_llm=fast_llm,
        analysis_llm=analysis_llm,
    )
    # Phase 2 K6: 生成済 synthesis を brief に配信 (dry-run は永続化しないので対象外)。
    if not dry_run and repo is not None:
        await _deliver_synthesis_discord(config, repo, result.generated)
        # Phase 4 FC2: 週次 synthesis 時に forecast indicator を snapshot + 前期検証
        # (spike から導いた監視指標の的中率 accountability ループ)。
        if result.generated.get("weekly"):
            try:
                from src.forecast.service import snapshot_and_verify_indicators

                snap, ver = snapshot_and_verify_indicators(repo, period_type="weekly")
                _log.info("forecast_indicators_updated", snapshotted=snap, verified=ver)
            except Exception as e:  # noqa: BLE001
                _log.error("forecast_indicator_snapshot_failed", error=str(e))
    generated_count = sum(1 for v in result.generated.values() if v)
    return PipelineRunResult(
        total_fetched=0,
        summarized=generated_count,
        posted=generated_count,
        marked_read=0,
        errors=result.errors,
    )


def _compose_daily_brief(
    *,
    slot: str,
    narrative: str,
    pir_body: str,
    sources: list[Source],
    period_label: str,
    section_count: int,
    burst_body: str = "",
) -> BriefingMessage | None:
    """朝刊/夕刊: daily synthesis ナラティブ (+ morning は PIR focus) を 1 BriefingMessage に合成。

    slot='morning' → 「朝ブリーフィング」(synthesis + PIR focus)、'evening' → 「夕ブリーフィング」
    (synthesis のみ = 夕方の状況更新)。朝刊/夕刊のように同じ「ブリーフィング」で中身が異なる。
    両方空なら None (投稿しない)。純粋関数 (LLM/DB/IO なし) でテスト容易。
    """
    parts = [p for p in (narrative.strip(), burst_body.strip(), pir_body.strip()) if p]
    if not parts:
        return None
    combined = "\n\n━━━━━━━━━━\n\n".join(parts)
    is_morning = slot == "morning"
    title = f"{'朝' if is_morning else '夕'}ブリーフィング ({period_label})"
    bluf = (
        f"状況総括 + PIR {section_count} 領域の 24h focus"
        if is_morning
        else "状況総括 (夕方の状況更新)"
    )
    return BriefingMessage(
        title=title,
        bluf=bluf,
        importance="high",
        category="daily_brief",
        summary=combined,
        sources=sources,
        metadata={
            "target_channel": "brief",
            "intel_kind": "daily_brief",
            "brief_slot": slot,
            "pir_section_count": section_count,
            "routing_reason": f"daily_brief:{slot}",
            "routing_rule_id": "DAILY_BRIEF",
        },
    )


# Web の日次ブリーフページ (frontend App.tsx の daily-brief route)。
_DAILY_BRIEF_WEB_PATH = "/app/daily-brief"


def _compose_compact_summary(
    *,
    narrative: str,
    pir_body: str,
    high_threats: str = "",
    burst: str = "",
    base_url: str | None,
) -> str:
    """Discord 用の要点射影を合成する (純粋関数)。

    全文 push (毎朝 14k 字 = 4-5 連投) をやめ、要点 + Web 誘導の 1 通にする
    (2026-07-12)。全文は daily_briefs に永続化済みで Web で pull 閲覧する。
    base_url が解決できれば誘導リンクを付け、無ければ文言のみに fallback。

    high_threats = 高脅威 Recall 安全網 (2026-07-16)。high 判定 web-only 脅威を必ず
    日次通読に載せる (分類と配信の断絶の解消)。synthesis/PIR より前に置く = 「act now」
    の tier を先頭に。
    """
    parts = [
        p for p in (high_threats.strip(), burst.strip(), narrative.strip(), pir_body.strip()) if p
    ]
    if base_url:
        parts.append(f"📎 全文 (根拠・記事一覧): {base_url}{_DAILY_BRIEF_WEB_PATH}")
    else:
        parts.append("📎 全文 (根拠・記事一覧) は Web「日次ブリーフ」で")
    return "\n\n".join(parts)


async def _run_daily_brief_default(
    *,
    config: AppConfig,
    pipeline: PipelineConfig,
    dry_run: bool,
    run_id: int | None = None,
) -> PipelineRunResult:
    """朝刊/夕刊 統合 #brief。slot は pipeline 名 (morning/evening) で判定。

    両 slot とも ① daily synthesis(31B) ナラティブを生成し status_synthesis に永続化、配信は
    本統合で実施。morning (朝刊) のみ ② PIR daily focus ドリルダウン(26B) + PIR-persist を併合。
    ③ 1 つの BriefingMessage (朝/夕 ブリーフィング) に合成して #brief に 1 投稿。
    朝刊=synthesis+PIR focus / 夕刊=synthesis のみ (夕方の状況更新)。
    """
    from src.digest.pir_daily_focus import _today_label
    from src.synthesis.discord import build_synthesis_compact, build_synthesis_message
    from src.synthesis.generator import generate_synthesis
    from src.tools.public_url import resolve_public_base_url

    slot = "evening" if "evening" in pipeline.name else "morning"
    is_morning = slot == "morning"
    period_label = _today_label()
    repo = None if dry_run else RunHistoryRepository()

    # ① ナラティブ (daily synthesis)。散文=narrative ティア / 台帳 ACH=reasoning ティア。
    syn_llm = build_llm_for(Step.SYNTHESIS_NARRATIVE, config)
    analysis_llm = build_llm_for(Step.SYNTHESIS_ANALYSIS, config)
    # 入力の多い triage (detect-new) は fast ティア 26B。
    fast_llm = build_llm_for(Step.SYNTHESIS_DETECT, config)
    narrative = ""
    syn_record = None
    try:
        syn_result = await generate_synthesis(
            llm=syn_llm, period_type="daily", fast_llm=fast_llm, analysis_llm=analysis_llm
        )
        if syn_result.record is not None:
            syn_record = syn_result.record
            if repo is not None:
                repo.upsert_status_synthesis(syn_record)
            narrative = build_synthesis_message(syn_record).summary
    except Exception as e:  # noqa: BLE001 — ナラティブ失敗で brief 全体を止めない
        _log.error("daily_brief_synthesis_failed", slot=slot, error=str(e))

    # ② PIR ドリルダウン+persist は morning (朝刊) のみ。evening (夕刊) は状況更新に絞る。
    pir_body = ""
    pir_compact = ""
    sources: list[Source] = []
    sections: list[Any] = []
    if is_morning:
        from src.digest.pir_daily_focus import (
            _format_full_digest,
            collect_pir_focus_sections,
            format_compact_digest,
        )

        focus_llm = build_llm_for(Step.PIR_DAILY_FOCUS, config)
        sections = await collect_pir_focus_sections(llm=focus_llm, lookback_hours=24)
        # PIR-persist rebuild は夜間独立ジョブ "pir-entity-rebuild" へ移設 (2026-07-06)。
        pir_body = _format_full_digest(sections, period_label) if sections else ""
        pir_compact = format_compact_digest(sections, period_label) if sections else ""
        for s in sections:
            for m in s.matches:
                sources.append(Source(title=m.feed_title or "source", url=m.url, language="auto"))
                if len(sources) >= 5:
                    break
            if len(sources) >= 5:
                break

    # ロードマップ D: 日次バースト検出 (I&W 統計シグナル、forecast の日次拡張)。
    # 決定論・LLM 追加呼出なし・DB read (+ 非 dry-run のみ indicator 起票)。
    # 失敗しても brief 全体を止めない (burst 側も fail-open の二重防御)。
    burst_section = ""
    if is_morning:
        try:
            from src.forecast.burst import detect_daily_bursts, format_burst_compact

            bursts = detect_daily_bursts(RunHistoryRepository(), persist=not dry_run)
            burst_section = format_burst_compact(bursts)
        except Exception as e:  # noqa: BLE001 — I&W 節の失敗で朝刊を止めない
            _log.error("daily_brief_burst_failed", error=str(e))

    message = _compose_daily_brief(
        slot=slot,
        narrative=narrative,
        pir_body=pir_body,
        sources=sources,
        period_label=period_label,
        section_count=len(sections),
        burst_body=burst_section,
    )
    if message is None:
        _log.info("daily_brief_empty", slot=slot, pipeline=pipeline.name)
        return PipelineRunResult(total_fetched=0, summarized=0, posted=0, errors=[])

    # 高脅威 Recall 安全網 (2026-07-16): high 判定で alert 未 push の脅威を必ず日次通読に
    # 載せる。lookback は slot 分担で重複回避 (朝=24h / 夕=morning 以降 ~13h)。決定論・
    # LLM 追加呼出なし。dry_run でも算出 (repo 不要、DB read のみ)。
    high_threat_compact = ""
    try:
        from src.digest.high_threat_digest import (
            collect_high_threats,
            format_high_threat_compact,
        )

        _ht_lookback = 24 if is_morning else 13
        _ht_items, _ht_total = collect_high_threats(lookback_hours=_ht_lookback)
        high_threat_compact = format_high_threat_compact(
            _ht_items, total=_ht_total, base_url=resolve_public_base_url()
        )
    except Exception as e:  # noqa: BLE001 — 安全網の失敗で brief 全体を止めない
        _log.error("daily_brief_high_threat_failed", slot=slot, error=str(e))

    # Discord には要点射影 (BLUF) のみ、全文は daily_briefs → Web で pull (2026-07-12)。
    compact_summary = _compose_compact_summary(
        narrative=build_synthesis_compact(syn_record) if syn_record is not None else "",
        pir_body=pir_compact,
        high_threats=high_threat_compact,
        burst=burst_section,
        base_url=resolve_public_base_url(),
    )

    if dry_run:
        _log.info(
            "daily_brief_dry_run",
            slot=slot,
            chars=len(message.summary),
            compact_chars=len(compact_summary),
            sections=len(sections),
            has_narrative=bool(narrative),
        )
        return PipelineRunResult(total_fetched=len(sections), summarized=1, posted=0, errors=[])

    # W1: 合成ブリーフ本文を永続化 (Web「日次ブリーフ」ビューで pull 閲覧)。Discord 投稿の前に
    # 保存し push 失敗でも本文を Web に残す。dry_run は repo=None で skip。
    if repo is not None:
        try:
            from src.digest.brief_payload import build_brief_payload

            repo.record_daily_brief(
                run_id=run_id,
                slot=slot,
                period_label=period_label,
                title=message.title,
                bluf=message.bluf,
                summary=message.summary,
                payload=build_brief_payload(syn_record=syn_record, sections=sections),
                section_count=len(sections),
                sources=[{"title": s.title, "url": s.url} for s in message.sources],
            )
        except Exception as e:  # noqa: BLE001 — 永続化失敗で配信を止めない
            _log.error("daily_brief_persist_failed", slot=slot, error=str(e))

    # 通知再設計: 配信先は product_routing (情報フロー編集可)。push=False の channel なら
    # 永続化のみで Discord 配信はスキップ (Web「日次ブリーフ」で pull 閲覧)。
    channel = product_channel("evening_brief" if slot == "evening" else "morning_brief")
    if not push_map().get(channel, True):
        _log.info("daily_brief_web_only", slot=slot, channel=channel)
        return PipelineRunResult(total_fetched=len(sections), summarized=1, posted=0, errors=[])
    publisher = _build_publishers(config).get(channel)
    if publisher is None:
        _log.error("daily_brief_publisher_missing", slot=slot, channel=channel)
        return PipelineRunResult(
            total_fetched=len(sections),
            summarized=1,
            posted=0,
            errors=[f"publisher_missing:{channel}"],
        )
    try:
        await publisher.post(message.model_copy(update={"summary": compact_summary}))
    except Exception as e:  # noqa: BLE001
        _log.error("daily_brief_post_failed", slot=slot, error=str(e))
        return PipelineRunResult(
            total_fetched=len(sections),
            summarized=1,
            posted=0,
            errors=[f"discord_post: {type(e).__name__}: {e}"],
        )
    _log.info(
        "daily_brief_posted",
        slot=slot,
        chars=len(message.summary),
        compact_chars=len(compact_summary),
        sections=len(sections),
        has_narrative=bool(narrative),
    )
    return PipelineRunResult(total_fetched=len(sections), summarized=1, posted=1, errors=[])


async def _run_pir_spotlight_default(
    *,
    config: AppConfig,
    pipeline: PipelineConfig,
    dry_run: bool,
    run_id: int | None = None,
) -> PipelineRunResult:
    """Phase Diamond verify-spotlight: PIR Spotlight pipeline。

    config/delivery/pir.yaml の spotlight.enabled=true な PIR each に対して narrative
    生成。timeout は per-PIR LLM call で 600s 設定 (synthesis より軽量、26B/31B
    どちらでも完走想定)。
    """
    _ = run_id
    from src.spotlight.runner import run_pir_spotlights

    # PIR spotlight narrative は fast ティア (600s, 26B/31B どちらでも完走想定)
    llm = build_llm_for(Step.PIR_SPOTLIGHT, config)
    _log.info(
        "spotlight_pipeline_start",
        pipeline=pipeline.name,
        model=llm.model,
        timeout_seconds=600.0,
    )
    repo: RunHistoryRepository | None = None if dry_run else RunHistoryRepository()
    # pipelines.yaml の source.synthesis_periods で period_type を override 可能
    # (未指定なら weekly)
    period = "weekly"
    if pipeline.source.synthesis_periods:
        for p in pipeline.source.synthesis_periods:
            if p in ("daily", "weekly", "monthly"):
                period = p
                break
    result = await run_pir_spotlights(
        llm=llm,
        repo=repo,
        period_type=period,  # type: ignore[arg-type]
    )
    # Phase 2 K6: 生成済 spotlight を watch に配信 (dry-run は永続化しないので対象外)。
    if not dry_run and repo is not None:
        await _deliver_spotlight_discord(config, repo, result.generated, period)
    return PipelineRunResult(
        total_fetched=0,
        summarized=len(result.generated),
        posted=len(result.generated),
        marked_read=0,
        errors=result.errors,
    )


# ---------- Phase H: taxonomy review pipeline runner ----------


async def _run_taxonomy_review_default(
    *,
    config: AppConfig,
    pipeline: PipelineConfig,
    dry_run: bool,
    run_id: int | None = None,
) -> PipelineRunResult:
    """Phase H: weekly-taxonomy-review pipeline。

    8 パターンの提案を生成 → taxonomy_review_proposals テーブルに永続化。
    Discord 配信なし (UI のみ)。LLM 呼出は MVP では pattern 4/5 の機械検出のみ、
    LLM ベース提案 (pattern 1) は Phase 3 で追加。
    """
    _ = config
    from src.taxonomy.runner import run_taxonomy_review

    repo: RunHistoryRepository | None = None if dry_run else RunHistoryRepository()
    lookback_days = max(
        1,
        pipeline.source.digest_lookback_hours // 24
        if pipeline.source.digest_lookback_hours
        else 30,
    )
    result = await run_taxonomy_review(
        repo=repo,
        run_id=run_id,
        lookback_days=lookback_days,
    )

    # 起票の ops 通知 (2026-07-31 運用レビュー完全調査): actor 提案と同じ病理で、
    # pending 提案は通知が無いと気づかれず滞留する (実例: 07-18 起票が 13 日未レビュー)。
    if result.new_proposals > 0 and not dry_run:
        try:
            from src.ui.services.ops_notify import post_ops_message

            await post_ops_message(
                title=f"taxonomy レビュー待ち: 新規提案 {result.new_proposals} 件",
                body=(
                    f"週次 taxonomy レビューで分類辞書の改善提案を"
                    f" {result.new_proposals} 件起票しました。運用タブの"
                    " Taxonomy Review パネルでレビューしてください。"
                ),
            )
        except Exception as e:  # noqa: BLE001 — 通知失敗でレビュー結果を捨てない
            _log.warning("taxonomy_proposal_ops_notify_failed", error=str(e))

    return PipelineRunResult(
        total_fetched=result.total_generated,
        summarized=result.new_proposals,
        posted=result.refreshed_proposals,  # 「reuse」相当を posted に流用
        marked_read=0,
        errors=result.errors,
    )


# ---------- 較正格子 P1: 遅延正解ラベルの週次収穫 ----------


async def _run_tuning_label_harvest_default(
    *,
    config: AppConfig,
    pipeline: PipelineConfig,
    dry_run: bool,
    run_id: int | None = None,
) -> PipelineRunResult:
    """weekly-tuning-label-harvest pipeline (docs/self_evolving_tuning_design.md §6 P1)。

    E1 (feed 突合) + E3 (taxonomy 採否 / editorial 訂正) を tuning_labels へ sweep する。
    LLM 呼出なし・Discord 記事配信なし。ops 通知は 0 件でも毎回 1 通送る
    (「届くこと自体が監査の生存証明」— fill-rate 監査と同じ方針、§12.3)。
    """
    _ = config, pipeline, run_id
    from src.tuning.label_harvest import run_label_harvest

    repo = RunHistoryRepository()
    result = run_label_harvest(repo, dry_run=dry_run)

    # §13 対処 A: 主体の決定論補完 (週次の安全網 — 主経路は ransomware_ingest 直後)
    from src.tuning.subject_backfill import run_subject_backfill

    backfill = run_subject_backfill(repo, dry_run=dry_run)

    if not dry_run:
        try:
            from src.tuning.supply_sentinel import supply_drift_lines
            from src.ui.services.ops_notify import post_ops_message

            # §13-1 残対処: E1 供給のドリフト番兵 (閉ループの静かな縮退を可視化)
            sentinel = "\n".join(supply_drift_lines(repo))
            await post_ops_message(
                title=f"tuning ラベル収穫: 新規 {result.total_new} 件",
                body=(
                    f"週次の遅延正解収穫 (較正格子 P1) — 主体突合 {result.feed_subject_new} 件"
                    f" (声明競合スキップ {result.feed_subject_conflicts} 件 /"
                    f" 自己ソーススキップ {result.feed_subject_self_source_skipped} 件) /"
                    f" taxonomy 裁定 {result.taxonomy_new} 件 /"
                    f" 論調訂正 {result.editorial_new} 件。"
                    f" エラー {len(result.errors)} 件。\n"
                    f"主体の決定論補完 (feed_match): {backfill.filled} 件"
                    f" (競合スキップ {backfill.skipped_conflict})。"
                    f" 既存ラベルの自己ソース再分類 (不変条件14): "
                    f"{result.self_source_reclassified} 件。"
                    + (f"\n{sentinel}" if sentinel else "")
                ),
            )
        except Exception as e:  # noqa: BLE001 — 通知失敗で収穫結果を捨てない
            _log.warning("label_harvest_ops_notify_failed", error=str(e))

    return PipelineRunResult(
        total_fetched=result.total_new + result.feed_subject_conflicts,
        summarized=result.total_new,
        posted=0,
        marked_read=0,
        errors=result.errors,
    )


# ---------- Actors Stage 4: MITRE actor sync pipeline runner ----------


async def _run_mitre_actor_sync_default(
    *,
    config: AppConfig,
    dry_run: bool,
    run_id: int | None = None,
) -> PipelineRunResult:
    """Actors Stage 4: MITRE ATT&CK → Actor 辞書の逐次同期。

    追加系差分 (alias/malware/references/summary 和訳) は自動適用 (atomic write + .bak、
    稼働アプリの git commit は廃止済)、判断が必要な差分 (新規 actor / alias 衝突) は
    actor_update_proposals に永続化して UI (Actor 辞書ページ) でレビューする。
    """
    from pathlib import Path

    from src.cti.mitre_sync import run_mitre_actor_sync
    from src.ui.services.file_editor import FileEditor

    llm = build_llm_for(Step.ACTOR_SYNC, config)
    repo: RunHistoryRepository | None = None if dry_run else RunHistoryRepository()
    editor = FileEditor(Path.cwd())

    def _write_yaml(content: str, commit_message: str) -> None:
        editor.write_file(
            "config/cti/actor_aliases.yaml",
            content,
            kind="yaml",
            commit_message=commit_message,
        )

    result = await run_mitre_actor_sync(
        llm=llm,
        repo=repo,
        run_id=run_id,
        dry_run=dry_run,
        write_yaml=_write_yaml,
    )
    # Actor Recall Layer (Part C2): MITRE 同期に相乗りしてコーパス由来の新興アクター候補も
    # 裏取り集計 → proposal キューに上げる (同じ承認 UI でレビュー)。MITRE と独立に失敗を握る。
    emerging_new = 0
    if repo is not None and not dry_run:
        try:
            from src.cti.actor_candidate_proposals import propose_emerging_actors

            stats = propose_emerging_actors(repo, run_id=run_id)
            emerging_new = stats["proposed"]
        except Exception as e:  # noqa: BLE001 — 集計失敗で MITRE 同期結果を捨てない
            _log.warning("emerging_actor_proposal_failed", error=str(e))
    # F3 (アクター辞書 Phase2): 報道の aka 併記から既知アクターの未収録別名を収穫し
    # 同じ承認キューに上げる (identity 昇格は人承認のみ)。これも独立に失敗を握る。
    news_alias_new = 0
    if repo is not None and not dry_run:
        try:
            from src.cti.news_alias_harvest import propose_news_aliases

            na_stats = propose_news_aliases(repo)
            news_alias_new = na_stats["proposed"]
        except Exception as e:  # noqa: BLE001
            _log.warning("news_alias_proposal_failed", error=str(e))
    # 起票の ops 通知 (2026-07-31): pending 提案は通知が無いと気づかれず滞留する
    # (実例: 07-28 起票の 5 件が 3 日間未レビュー)。新規起票のあった週次同期後に 1 行
    # 通知してレビュー UI (アクター辞書ページ) へ誘導する。通知失敗は同期結果を汚さない。
    proposals_total = result.proposals_new + emerging_new + news_alias_new
    if proposals_total > 0 and not dry_run:
        try:
            from src.ui.services.ops_notify import post_ops_message

            await post_ops_message(
                title=f"アクター辞書レビュー待ち: 新規提案 {proposals_total} 件",
                body=(
                    f"週次同期で辞書昇格・別名の提案を {proposals_total} 件起票しました"
                    f" (MITRE {result.proposals_new} / 新興 {emerging_new}"
                    f" / 別名 {news_alias_new})。"
                    "アクター辞書ページの同期提案パネルでレビューしてください。"
                ),
            )
        except Exception as e:  # noqa: BLE001 — 通知失敗で同期結果を捨てない
            _log.warning("actor_proposal_ops_notify_failed", error=str(e))
    return PipelineRunResult(
        total_fetched=result.groups_total,
        summarized=result.auto_applied,
        # 「レビュー待ち提案」件数を posted に流用
        posted=proposals_total,
        marked_read=0,
        errors=result.errors,
    )
