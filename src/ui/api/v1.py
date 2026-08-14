"""Intel Graph React SPA 用 JSON API (v1)。

エンドポイント:
    GET  /api/v1/intel-graph/snapshot          — shell 用 (families/nations/discovery/filters)
    GET  /api/v1/intel-graph/threats           — threats tab (actors list + optional detail)
    GET  /api/v1/intel-graph/threats/actor/{id}— actor detail (timeline 含む)
    GET  /api/v1/intel-graph/pmesii            — pmesii tab (cards + synthesis context)
    GET  /api/v1/intel-graph/synthesis         — synthesis tab (period_type 別)
    GET  /api/v1/intel-graph/daily-briefs      — 日次ブリーフ (朝刊/夕刊) の Web 通読 (W1)
    WS   /ws/v1/events                          — real-time push (新 article / synthesis 完了 等)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository, StatusSynthesisRecord
from src.ui.services.actor_threat import (
    ActorThreatAssessment,
    fetch_mission_threat_assessments,
)
from src.ui.services.threat_operations import (
    ActorActivity,
    actor_canonical_lookup,
    fetch_actor_detail,
    fetch_threat_operations_snapshot,
)

_log = get_logger(__name__)

api = APIRouter(prefix="/api/v1/intel-graph", tags=["intel-graph"])


def _truthy(v: str | None) -> bool:
    if v is None:
        return False
    return v.strip().lower() not in ("", "0", "false", "off", "no")


def _parse_lookback(time: str | None) -> int:
    try:
        v = int(str(time or "30"))
    except ValueError:
        return 30
    return max(1, min(730, v))


@api.get("/snapshot")
def get_snapshot(  # sync def = FastAPI が threadpool 実行 (重い集計で event loop を塞がない)
    time: str = Query(default="30"),
    nation: str = "",
    family: str = "",
    japan_only: str = "",
    high_only: str = "",
    search: str = "",
    pir: str = "",
) -> dict[str, Any]:
    """Shell + Discovery panel 用 snapshot (filters の影響 + families/nations 列挙)。"""
    lookback = _parse_lookback(time)
    snap = fetch_threat_operations_snapshot(
        lookback_days=lookback,
        nation_filter=nation or None,
        family_filter=family or None,
        japan_targeted_only=_truthy(japan_only),
        high_importance_only=_truthy(high_only),
        search_query=search or None,
        pir_filter=pir or None,
    )
    return {
        "lookback_days": lookback,
        "families": snap.families,
        "nations": list(snap.nations),
        "filters": snap.filters,
        "actor_lookup": actor_canonical_lookup(),
        "discovery": {
            "new_actors": [_actor_brief(a) for a in snap.discovery.new_actors],
            "spiking_actors": [_actor_brief(a) for a in snap.discovery.spiking_actors],
            "waking_actors": [_actor_brief(a) for a in snap.discovery.waking_actors],
            "unknown_bucket_count": snap.discovery.unknown_bucket_count,
        },
        "actors_count": len(snap.actors),
    }


def _actor_brief(a: ActorActivity) -> dict[str, Any]:
    """ActorActivity から軽量な dict (list 表示用)。"""
    return {
        "actor_id": a.actor_id,
        "canonical": a.canonical,
        "aliases": list(a.aliases),
        "nation": a.nation,
        "family": a.family,
        "mitre_group": a.mitre_group,
        # Actors Stage 5: 実体種別 (group/organization/contractor) と親機関
        "kind": a.kind,
        "sponsor_org": a.sponsor_org,
        "total_articles": a.total_articles,
        "daily_counts": list(a.daily_counts),
        "sparkline": a.sparkline,
        "is_new": a.is_new,
        "is_spike": a.is_spike,
        "spike_ratio": a.spike_ratio if a.spike_ratio != float("inf") else None,
        "is_quiet_waking": a.is_quiet_waking,
        "japan_targeted_count": a.japan_targeted_count,
        "last_seen_iso": a.last_seen_iso,
        # Phase Diamond-Axes: socio-political 軸 (この actor の意図分布、[(intent, n), ...])
        "top_intents": [list(t) for t in a.top_intents],
        # ③ PIR 連携: この actor が該当する enabled PIR の id 群 (UI バッジ/優先用)
        "matched_pir_ids": list(a.matched_pir_ids),
    }


def _threat_dict(a: ActorThreatAssessment | None) -> dict[str, Any] | None:
    """ミッション脅威評価 → JSON (None = 未評価対象外 or 評価不可)。"""
    if a is None:
        return None
    return {
        "tier": a.tier,
        "tier_rule": a.tier_rule,
        "relevance_band": a.relevance_band,
        "capability_band": a.capability_band,
        "activity_state": a.activity_state,
        "relevance_factors": list(a.relevance_factors),
        "capability_factors": list(a.capability_factors),
        "activity_factors": list(a.activity_factors),
        "coverage_note": a.coverage_note,
    }


def _safe_assessments() -> dict[str, ActorThreatAssessment]:
    """評価 map (90d 固定窓、TTL cache)。障害時は空 = チップ非表示に degrade。"""
    try:
        return fetch_mission_threat_assessments()
    except Exception as e:  # noqa: BLE001 — 評価障害で Threats page を殺さない
        _log.warning("mission_threat_assessments_failed", error=str(e))
        return {}


@api.get("/threats")
def get_threats(  # sync def = threadpool 実行 (同上)
    time: str = "30",
    nation: str = "",
    family: str = "",
    japan_only: str = "",
    high_only: str = "",
    search: str = "",
    pir: str = "",
) -> dict[str, Any]:
    """Threats tab actor list (filtered)。

    include_dormant=True で休眠アクター (期間内観測 0) も返す — ミッション脅威評価
    (threat) が Critical・休眠を可視化するため。評価は 90d 固定窓 (UI 窓と独立)。
    """
    lookback = _parse_lookback(time)
    snap = fetch_threat_operations_snapshot(
        lookback_days=lookback,
        nation_filter=nation or None,
        family_filter=family or None,
        japan_targeted_only=_truthy(japan_only),
        high_importance_only=_truthy(high_only),
        search_query=search or None,
        pir_filter=pir or None,
        include_dormant=True,
    )
    assessments = _safe_assessments()
    return {
        "actors": [
            _actor_brief(a) | {"threat": _threat_dict(assessments.get(a.actor_id))}
            for a in snap.actors
        ],
        "discovery": {
            "new_actors": [_actor_brief(a) for a in snap.discovery.new_actors],
            "spiking_actors": [_actor_brief(a) for a in snap.discovery.spiking_actors],
            "waking_actors": [_actor_brief(a) for a in snap.discovery.waking_actors],
            "unknown_bucket_count": snap.discovery.unknown_bucket_count,
        },
    }


@api.get("/threats/actor/{actor_id}")
def get_actor_detail(  # sync def = threadpool 実行 (同上)
    actor_id: str, time: str = "30"
) -> dict[str, Any]:
    """Actor 詳細 (stats + timeline + recent articles + relations)。"""
    lookback = _parse_lookback(time)
    detail = fetch_actor_detail(actor_id, lookback_days=lookback)
    if detail is None:
        return {"actor_id": actor_id, "found": False}
    return {
        "actor_id": actor_id,
        "found": True,
        "activity": _actor_brief(detail.activity)
        | {"threat": _threat_dict(_safe_assessments().get(actor_id))}
        | {
            "sponsor": detail.activity.sponsor,
            "description": detail.activity.description,
            "top_sectors": [list(t) for t in detail.activity.top_sectors],
            "top_countries": [list(t) for t in detail.activity.top_countries],
            "top_cves": list(detail.activity.top_cves),
            "top_ttps": list(detail.activity.top_ttps),
            # Phase Diamond: Capability + Infrastructure 軸の構造化集計
            "top_malware_families": [list(t) for t in detail.activity.top_malware_families],
            "top_tools": [list(t) for t in detail.activity.top_tools],
            "top_iocs_ip": [list(t) for t in detail.activity.top_iocs_ip],
            "top_iocs_domain": [list(t) for t in detail.activity.top_iocs_domain],
            "top_iocs_hash": [list(t) for t in detail.activity.top_iocs_hash],
            "top_iocs_url": [list(t) for t in detail.activity.top_iocs_url],
        },
        "relations": {
            "family_members": [list(t) for t in detail.relations.family_members],
            "cooccur_actors": [list(t) for t in detail.relations.cooccur_actors],
            "related_campaigns": list(detail.relations.related_campaigns),
        },
        "recent_articles": [
            {
                "article_id": r.article_id,
                "title": r.title,
                "url": r.url,
                "feed_title": r.feed_title,
                "importance": r.importance,
                "created_at": r.created_at,
                "posted_channel": r.posted_channel,
                # Phase Diamond-Axes: incident 単位の 2 軸
                "socio_political_intent": r.socio_political_intent,
                "technical_axis_summary": r.technical_axis_summary,
            }
            for r in detail.recent_articles
        ],
        "timeline_daily": [list(t) for t in detail.timeline_daily],
        # 時間軸トグル: 発生時刻系列 + カバレッジ (報道時刻 timeline_daily と切替え)。
        "timeline_event": [list(t) for t in detail.timeline_event],
        "timeline_coverage": {
            "dated": detail.timeline_coverage[0],
            "total": detail.timeline_coverage[1],
            "event_in_window": detail.timeline_coverage[2],
        },
        # 既知 TTP (Actor 辞書由来、knowledge)。観測 top_ttps との gap 表示用
        "known_ttps": list(detail.known_ttps),
        # 配下グループ rollup (organization のみ): [actor_id, canonical, count, sparkline]
        "child_groups": [list(t) for t in detail.child_groups],
    }


@api.get("/pmesii")
async def get_pmesii(
    time: str = "30",
    axis: str = "",
) -> dict[str, Any]:
    """PMESII axes cards + synthesis context bar 用 data。"""
    from src.ui.routers.intel_graph import _build_pmesii_cards

    lookback = _parse_lookback(time)
    lookback_hours = lookback * 24
    baseline_weeks = max(1, min(8, lookback // 7))
    cards, non_empty = _build_pmesii_cards(
        lookback_hours=lookback_hours,
        baseline_weeks=baseline_weeks,
        focused_axis=axis,
    )
    # synthesis context
    repo = RunHistoryRepository()
    daily = repo.get_latest_synthesis(period_type="daily")
    weekly = repo.get_latest_synthesis(period_type="weekly")
    latest: StatusSynthesisRecord | None
    if daily is not None and (weekly is None or daily.generated_at >= weekly.generated_at):
        latest = daily
        kind = "daily"
    else:
        latest = weekly
        kind = "weekly"
    synth_ctx: dict[str, Any] | None = None
    if latest is not None:
        synth_ctx = {
            "kind": kind,
            "headline": latest.headline,
            "weight_section": latest.weight_section,
            # Phase Diamond fix: ISO 形式 (UTC tz info 込み) で返し、frontend で formatJst 経由表示
            # 旧 strftime は timezone 変換を行わず UTC を local 表記していたため 9 時間ずれていた
            "period_start": latest.period_start.isoformat(),
            "period_end": latest.period_end.isoformat(),
            "generated_at": latest.generated_at.isoformat(),
            "article_count": latest.article_count,
        }
    return {
        "cards": cards,
        "lookback_hours": lookback_hours,
        "baseline_weeks": baseline_weeks,
        "non_empty_count": non_empty,
        "focused_axis": axis,
        "synthesis": synth_ctx,
    }


@api.get("/situation/nations")
async def get_situation_nations(time: str = "90") -> dict[str, Any]:
    """国家中心 情勢ボードのセレクタ: サイバー/地政学を持つ国の件数一覧。"""
    from src.ui.services.situation import list_nations

    days = _parse_lookback(time)
    return {"nations": list_nations(window_days=days or None)}


@api.get("/situation")
async def get_situation(nation: str = "", time: str = "90") -> dict[str, Any]:
    """国家中心 情勢: サイバー面(APT) + 地政学面(当事国) + テンポ(日次)を相関して返す。"""
    from src.ui.services.situation import list_nations, situation_by_nation

    days = _parse_lookback(time)
    nat = nation.strip().upper()
    if not (len(nat) == 2 and nat.isalpha()):
        # 国未指定: セレクタ先頭の国を既定にする (空ボードを避ける)。
        nations = list_nations(window_days=days or None)
        if not nations:
            return {"nation": "", "label": "", "cyber": None, "geopolitical": None}
        nat = str(nations[0]["iso"])
    return situation_by_nation(nat, window_days=days or None)


@api.get("/synthesis")
async def get_synthesis(period_type: str = "weekly") -> dict[str, Any]:
    """Synthesis tab data。"""
    if period_type not in ("daily", "weekly", "monthly"):
        period_type = "weekly"
    repo = RunHistoryRepository()
    latest = repo.get_latest_synthesis(period_type=period_type)
    if latest is None:
        return {"period_type": period_type, "has_data": False}
    try:
        parsed = json.loads(latest.axes_evidence)
        axes_evidence = (
            {k: v for k, v in parsed.items() if isinstance(v, list)}
            if isinstance(parsed, dict)
            else {}
        )
    except (json.JSONDecodeError, ValueError):
        axes_evidence = {}
    # S1: 各 evidence の出典基盤 (信頼度) を source メタから決定的に付与する。
    from src.cti.source_basis import compute_source_basis
    from src.tools.kev_client import get_kev_cve_set

    kev_set = get_kev_cve_set()
    for entries in axes_evidence.values():
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            aids = ent.get("article_ids") or []
            sb = compute_source_basis(
                repo, [str(a) for a in aids if a] if isinstance(aids, list) else [], kev_set=kev_set
            )
            ent["source_basis"] = {
                "confidence": sb.confidence,
                "best_tier": sb.best_tier,
                "source_count": sb.source_count,
                "has_official_authority": sb.has_official_authority,
                "social_only": sb.social_only,
                "reason": sb.reason,
            }
    # S2: 分析トレードクラフト (主見立て+対立仮説+前提+覆る指標) を parse して返す。
    try:
        tradecraft = json.loads(latest.tradecraft) if latest.tradecraft else {}
        if not isinstance(tradecraft, dict):
            tradecraft = {}
    except (json.JSONDecodeError, ValueError):
        tradecraft = {}
    # 台帳現在値 overlay (2026-07-25 ACH 整合監査): 射影 (tradecraft) は生成時点の凍結
    # スナップショットで、台帳がその後是正されても古い判定を表示し続ける (週次で最大 1 週間)。
    # 各判定に最新 revision の現在値を併記し、UI が「台帳の現在値」を示せるようにする。
    # judgment の id は situation_id (台帳照合キー)。台帳未接続の旧形式判定は素通し。
    try:
        _ge = tradecraft.get("grounded_estimate")
        _judgments = _ge.get("judgments") if isinstance(_ge, dict) else None
        if isinstance(_judgments, list) and _judgments:
            from src.assessment.situation_store import SituationStore

            _store = SituationStore()
            for _j in _judgments:
                if not isinstance(_j, dict):
                    continue
                _sid = str(_j.get("id") or "")
                if not _sid.startswith("s-"):
                    continue
                _rev = _store.latest_revision(_sid)
                if _rev is None:
                    continue
                _j["ledger_now"] = {
                    "rev": _rev.rev,
                    "leading_hypothesis": _rev.leading_hypothesis,
                    "confidence": _rev.confidence,
                    "updated_at": _rev.created_at,
                    "differs": bool(
                        _rev.leading_hypothesis != _j.get("leading_hypothesis")
                        or _rev.confidence != _j.get("confidence")
                    ),
                }
    except Exception as e:  # noqa: BLE001 — overlay 失敗で synthesis 表示自体は壊さない
        _log.warning("synthesis_ledger_overlay_failed", error=str(e)[:150])
    # B(2): 直近 forecast_scorecard を集計し予測の的中率 (calibration) を返す。
    realized = partial = missed = unevaluated = 0
    try:
        for rec in repo.list_synthesis(period_type=period_type, limit=10):
            tc = json.loads(rec.tradecraft) if rec.tradecraft else {}
            for sc in (tc.get("forecast_scorecard") or []) if isinstance(tc, dict) else []:
                verdict = sc.get("verdict") if isinstance(sc, dict) else None
                if verdict == "realized":
                    realized += 1
                elif verdict == "partial":
                    partial += 1
                elif verdict == "missed":
                    missed += 1
                elif verdict == "unevaluated":
                    unevaluated += 1
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # 較正バグ修正 (2026-08-14): unevaluated は「情勢が再評価されず指標を一度も
    # 照会できなかった」= 構造的に hit 不能だった予測。外れとして分母に入れると
    # 的中率が体系的に過小評価される (詳細: assessment/forecast.py の module docstring)。
    # 分母から外し、除外件数は別項として返す (黙って落とさない)。
    scored = realized + partial + missed
    forecast_accuracy = {
        "realized": realized,
        "partial": partial,
        "missed": missed,
        "unevaluated": unevaluated,
        "scored": scored,
        # partial は半分カウント (realized + 0.5*partial) / scored。
        "hit_rate_pct": round(100 * (realized + 0.5 * partial) / scored) if scored else None,
    }
    return {
        "period_type": period_type,
        "has_data": True,
        "forecast_accuracy": forecast_accuracy,
        "latest": {
            "headline": latest.headline,
            "weight_section": latest.weight_section,
            "chain_section": latest.chain_section,
            "cog_section": latest.cog_section,
            "spillover_section": latest.spillover_section,
            "pir_section": latest.pir_section,
            # Phase Diamond fix: ISO 形式 (UTC tz info 込み) で返し、frontend で formatJst 経由表示
            # 旧 strftime は timezone 変換を行わず UTC を local 表記していたため 9 時間ずれていた
            "period_start": latest.period_start.isoformat(),
            "period_end": latest.period_end.isoformat(),
            "generated_at": latest.generated_at.isoformat(),
            "article_count": latest.article_count,
            "llm_model": latest.llm_model or "",
        },
        "axes_evidence": axes_evidence,
        "tradecraft": tradecraft,
    }


@api.get("/forecast")
async def get_forecast(weeks: int = Query(default=8, ge=4, le=26)) -> dict[str, Any]:
    """Phase 4 将来予測: spike (FC3) / トレンド (FC4) / 相関 (FC5) / 指標的中率 (FC2)。

    決定的 (LLM 非依存) な時系列分析。actor / intent の週次活動から、分散考慮 spike や
    トレンド方向、強相関ペアを算出し、過去の監視指標の的中率も返す。read-only。
    """
    from src.forecast.service import build_forecast

    repo = RunHistoryRepository()
    result = build_forecast(repo, weeks=weeks)
    return result.model_dump(mode="json")


@api.get("/retrospect")
async def get_retrospect(weeks_ago: int = Query(default=1, ge=0, le=52)) -> dict[str, Any]:
    """Phase 6 過去参照: ``weeks_ago`` 週前の振り返りスナップショット。

    その週の synthesis (状況総括) + 主要記事 + 活動 actor + forecast 指標の的中結果を
    既存データから再構成する time-machine。read-only。
    """
    from src.ui.services.retrospect import build_retrospect

    return build_retrospect(RunHistoryRepository(), weeks_ago=weeks_ago)


@api.get("/brief-context")
async def get_brief_context(until: str = Query(default="")) -> dict[str, Any]:
    """ブリーフ閲覧時の補足コンテキスト (時間軸統合 P2/P3)。

    ``until`` = 選択中ブリーフの生成時刻 (ISO)。省略時は現在時刻。直前 24h の
    活動アクターと、その週の weekly 予測指標 (観測中含む) を閲覧時計算で返す。
    配信物 (brief 本文) は不変の記録のまま — 文脈は読む画面が持つ。
    """
    from src.ui.services.retrospect import build_brief_context

    ts = datetime.now(UTC)
    if until.strip():
        try:
            parsed = datetime.fromisoformat(until.strip())
            ts = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"until は ISO 8601 形式で指定してください: {until[:40]}"
            ) from e

    return build_brief_context(RunHistoryRepository(), until=ts)


@api.get("/daily-briefs")
async def get_daily_briefs(
    limit: int = Query(default=30, ge=1, le=180),
    meta_only: int = Query(default=0, ge=0, le=1),
) -> dict[str, Any]:
    """W1 (通知再設計): 永続化済みの日次ブリーフ (朝刊/夕刊) を新しい順に返す。

    Discord に push される朝刊 (06:30) / 夕刊 (19:30) と同じ本文を Web で通読 (pull) する
    ための surface。read-only。meta_only=1 は一覧サイドバー用の軽量メタのみ (60 件で
    ~2MB になる本文全乗せを避ける。本文は /daily-briefs/{id} で選択時に取得)。
    """
    repo = RunHistoryRepository()
    return {"briefs": repo.list_daily_briefs(limit=limit, meta_only=bool(meta_only))}


@api.get("/daily-briefs/{brief_id}")
async def get_daily_brief_by_id(brief_id: int) -> dict[str, Any]:
    """1 件の日次ブリーフを本文込みで返す (一覧からの選択時オンデマンド取得)。"""
    repo = RunHistoryRepository()
    brief = repo.get_daily_brief(brief_id)
    if brief is None:
        raise HTTPException(status_code=404, detail=f"brief {brief_id} が見つかりません")
    return {"brief": brief}
