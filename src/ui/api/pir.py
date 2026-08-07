"""PIR 管理 API (Phase Diamond verify-pir-driven)。

CRUD + KPI + preview + LLM compile を提供。
読み取り (GET) は readonly instance でも許可、書き込み (POST/PUT/DELETE) は
既存 READ_ONLY middleware で 403 block される。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.config_loader import load_app_config
from src.logging_config import get_logger
from src.pir.compiler import CompileResult, compile_pir
from src.pir.evaluator import (
    PirMatch,
    compute_pir_kpi,
    evaluate_pir_matches,
    evaluate_pirs_batch,
)
from src.pir.integration import (
    load_current_pir_config,
    persist_pir_config,
)
from src.pir.models import (
    Pir,
)
from src.tools.model_tiers import Step, build_llm_for

_log = get_logger(__name__)

pir_api = APIRouter(prefix="/api/v1/pir", tags=["pir"])


# ---------- API models ----------


class PirListItem(BaseModel):
    """PIR 一覧用の軽量 representation (KPI snapshot 込み)。"""

    id: str
    title: str
    description: str
    enabled: bool
    target_importance: str
    spotlight_enabled: bool
    match_count_7d: int = 0
    match_count_30d: int = 0
    last_match_at: str | None = None
    approved_by_user: bool = True


class PirListResponse(BaseModel):
    version: int
    priorities: list[PirListItem]
    pending_draft_count: int


class CompileRequest(BaseModel):
    title: str
    description: str
    pir_id: str | None = None  # 編集中 PIR の id (新規作成は None)


class CompileResponse(BaseModel):
    pir: Pir
    confidence: dict[str, str]
    rationale: str
    warnings: list[str]


class PreviewRequest(BaseModel):
    pir: Pir
    lookback_hours: int = 168


class PreviewMatchSample(BaseModel):
    article_id: str
    title: str
    url: str
    feed_title: str
    importance: str
    posted_channel: str | None
    created_at: str
    matched_via: list[str]


class PreviewResponse(BaseModel):
    match_count: int
    samples: list[PreviewMatchSample]


class SavePirRequest(BaseModel):
    pir: Pir
    is_new: bool = False  # True = 新規追加、False = 既存 update


class KpiResponse(BaseModel):
    pir_id: str
    match_count_7d: int
    match_count_30d: int
    last_match_at: str | None
    top_feeds: list[tuple[str, int]]
    top_actors: list[tuple[str, int]]
    channel_distribution: dict[str, int]
    samples: list[PreviewMatchSample]


# ---------- helpers ----------


def _to_list_item(pir: Pir, matches_30d: list[PirMatch]) -> PirListItem:
    """Pir + 一覧に必要な件数だけを representation に変換。

    一覧では上位フィード/アクター等の詳細 KPI は使わないため、``compute_pir_kpi``
    (PIR ごとに 30 日分の全走査 + actor クエリ) は呼ばない。
    """
    cutoff_7d = datetime.now(UTC) - timedelta(days=7)
    count_7d = sum(1 for m in matches_30d if (_parse_dt(m.created_at) or cutoff_7d) >= cutoff_7d)
    return PirListItem(
        id=pir.id,
        title=pir.title,
        description=pir.description,
        enabled=pir.enabled,
        target_importance=pir.target_importance,
        spotlight_enabled=pir.spotlight.enabled,
        match_count_7d=count_7d,
        match_count_30d=len(matches_30d),
        last_match_at=matches_30d[0].created_at if matches_30d else None,
        approved_by_user=pir.metadata.approved_by_user,
    )


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _match_to_sample(match: Any) -> PreviewMatchSample:
    return PreviewMatchSample(
        article_id=match.article_id,
        title=match.title,
        url=match.url,
        feed_title=match.feed_title,
        importance=match.importance,
        posted_channel=match.posted_channel,
        created_at=match.created_at,
        matched_via=list(match.matched_via),
    )


# ---------- list / detail ----------


# 一覧は PIR 全件の 30 日マッチ件数を出すため、素朴に組むと重い。1 回の読込で全 PIR を
# 評価したうえで短 TTL キャッシュを重ね、連続表示を即時にする (件数は集計値なので
# 数十秒の遅れは許容)。dashboard/overview と同じ考え方。
_LIST_CACHE: dict[str, tuple[float, PirListResponse]] = {}
_LIST_TTL_SEC = 45.0


def _invalidate_pir_caches() -> None:
    """PIR 保存/削除の直後に一覧・ダッシュボードのキャッシュを捨てる。

    TTL 待ちにすると「保存したのに一覧が変わらない」ように見えるため、
    編集経路では明示的に落とす。
    """
    _LIST_CACHE.clear()
    _OVERVIEW_CACHE.clear()


# 同期関数として定義する (async def にすると 30 日走査の間 event loop を占有し、
# UI 全体が固まる)。FastAPI が threadpool で実行する。
@pir_api.get("", response_model=PirListResponse)
def list_pirs() -> PirListResponse:
    cached = _LIST_CACHE.get("list")
    if cached is not None and (time.monotonic() - cached[0]) < _LIST_TTL_SEC:
        return cached[1]
    cfg = load_current_pir_config()
    matches = evaluate_pirs_batch(cfg.priorities, lookback_hours=24 * 30, limit=15000)
    items = [_to_list_item(p, matches.get(p.id, [])) for p in cfg.priorities]
    pending = sum(1 for p in cfg.priorities if not p.metadata.approved_by_user)
    resp = PirListResponse(
        version=cfg.version,
        priorities=items,
        pending_draft_count=pending,
    )
    _LIST_CACHE["list"] = (time.monotonic(), resp)
    return resp


@pir_api.get("/{pir_id}", response_model=Pir)
async def get_pir(pir_id: str) -> Pir:
    cfg = load_current_pir_config()
    pir = cfg.find(pir_id)
    if pir is None:
        raise HTTPException(status_code=404, detail=f"PIR が見つかりません: {pir_id}")
    return pir


@pir_api.get("/{pir_id}/kpi", response_model=KpiResponse)
async def get_pir_kpi(pir_id: str) -> KpiResponse:
    cfg = load_current_pir_config()
    pir = cfg.find(pir_id)
    if pir is None:
        raise HTTPException(status_code=404, detail=f"PIR が見つかりません: {pir_id}")
    kpi = compute_pir_kpi(pir, sample_limit=10)
    return KpiResponse(
        pir_id=kpi.pir_id,
        match_count_7d=kpi.match_count_7d,
        match_count_30d=kpi.match_count_30d,
        last_match_at=kpi.last_match_at,
        top_feeds=kpi.top_feeds,
        top_actors=kpi.top_actors,
        channel_distribution=kpi.channel_distribution,
        samples=[_match_to_sample(m) for m in kpi.samples],
    )


# ---------- compile + preview ----------


@pir_api.post("/compile", response_model=CompileResponse)
async def compile_pir_endpoint(req: CompileRequest) -> CompileResponse:
    """LLM で description → structured fields を生成 (作成画面の "AI で構造化")。"""
    if not req.title.strip():
        raise HTTPException(status_code=400, detail="タイトルは必須です")
    if not req.description.strip():
        raise HTTPException(status_code=400, detail="説明は必須です")

    cfg = load_app_config()
    llm = build_llm_for(Step.PIR_COMPILE, cfg)
    pir_id = req.pir_id or _slugify_id(req.title)
    result: CompileResult = await compile_pir(
        pir_id=pir_id,
        title=req.title,
        description=req.description,
        enabled=True,
        llm=llm,
    )
    return CompileResponse(
        pir=result.pir,
        confidence=dict(result.confidence),
        rationale=result.rationale,
        warnings=result.warnings,
    )


@pir_api.post("/preview", response_model=PreviewResponse)
async def preview_pir(req: PreviewRequest) -> PreviewResponse:
    """draft PIR で実際に何件 match するか preview (Save 前の確認用)。"""
    matches = evaluate_pir_matches(req.pir, lookback_hours=req.lookback_hours, limit=1000)
    samples = [_match_to_sample(m) for m in matches[:10]]
    return PreviewResponse(match_count=len(matches), samples=samples)


# ---------- save / delete / approve ----------


@pir_api.post("/save", response_model=Pir)
async def save_pir(req: SavePirRequest) -> Pir:
    """新規追加 or 既存 update。"""
    # 照合条件の構造検証 (authoring 統一 §3.2)。evaluate_match は不明構造を静かに
    # no-match に倒すため、保存時に拒否して「静かな沈黙 PIR」を作らせない。
    if req.pir.match is not None:
        from src.pir.signal_match import validate_match_tree

        errors, _warnings = validate_match_tree(req.pir.match)
        if errors:
            raise HTTPException(
                status_code=400,
                detail="照合条件が不正です: " + " / ".join(errors[:5]),
            )
    cfg = load_current_pir_config()
    existing = cfg.find(req.pir.id)

    if req.is_new and existing is not None:
        raise HTTPException(status_code=409, detail=f"PIR は既に存在します: {req.pir.id}")
    if not req.is_new and existing is None:
        raise HTTPException(
            status_code=404, detail=f"更新対象の PIR が見つかりません: {req.pir.id}"
        )

    now = datetime.now(UTC)
    pir = req.pir
    if req.is_new:
        # 新規: created_at + updated_at
        pir = pir.model_copy(
            update={
                "metadata": pir.metadata.model_copy(
                    update={
                        "created_at": now,
                        "updated_at": now,
                        "approved_by_user": True,  # UI で save = user 承認とみなす
                    }
                ),
            }
        )
        cfg.priorities.append(pir)
    else:
        # update: 既存 PIR を replace (上のガードで existing は非 None が保証済み)
        assert existing is not None
        pir = pir.model_copy(
            update={
                "metadata": pir.metadata.model_copy(
                    update={
                        "created_at": existing.metadata.created_at or now,
                        "updated_at": now,
                        "approved_by_user": True,
                    }
                ),
            }
        )
        idx = next(i for i, p in enumerate(cfg.priorities) if p.id == pir.id)
        cfg.priorities[idx] = pir

    persist_pir_config(cfg)
    _invalidate_pir_caches()
    _log.info("pir_saved", pir_id=pir.id, is_new=req.is_new)
    return pir


@pir_api.post("/{pir_id}/approve", response_model=Pir)
async def approve_pir(pir_id: str) -> Pir:
    """draft PIR を user 承認済としてマーク (UI の一覧で banner を消す用)。"""
    cfg = load_current_pir_config()
    pir = cfg.find(pir_id)
    if pir is None:
        raise HTTPException(status_code=404, detail=f"PIR が見つかりません: {pir_id}")
    if pir.metadata.approved_by_user:
        return pir
    updated = pir.model_copy(
        update={
            "metadata": pir.metadata.model_copy(
                update={
                    "approved_by_user": True,
                    "updated_at": datetime.now(UTC),
                }
            ),
        }
    )
    idx = next(i for i, p in enumerate(cfg.priorities) if p.id == pir_id)
    cfg.priorities[idx] = updated
    persist_pir_config(cfg)
    _invalidate_pir_caches()
    return updated


@pir_api.post("/{pir_id}/toggle", response_model=Pir)
async def toggle_pir(pir_id: str) -> Pir:
    """PIR の enabled を toggle (一覧画面で素早く on/off)。"""
    cfg = load_current_pir_config()
    pir = cfg.find(pir_id)
    if pir is None:
        raise HTTPException(status_code=404, detail=f"PIR が見つかりません: {pir_id}")
    updated = pir.model_copy(
        update={
            "enabled": not pir.enabled,
            "metadata": pir.metadata.model_copy(update={"updated_at": datetime.now(UTC)}),
        }
    )
    idx = next(i for i, p in enumerate(cfg.priorities) if p.id == pir_id)
    cfg.priorities[idx] = updated
    persist_pir_config(cfg)
    _invalidate_pir_caches()
    return updated


@pir_api.delete("/{pir_id}", response_model=dict)
async def delete_pir(pir_id: str) -> dict[str, Any]:
    cfg = load_current_pir_config()
    pir = cfg.find(pir_id)
    if pir is None:
        raise HTTPException(status_code=404, detail=f"PIR が見つかりません: {pir_id}")
    cfg.priorities = [p for p in cfg.priorities if p.id != pir_id]
    persist_pir_config(cfg)
    _invalidate_pir_caches()
    return {"deleted": True, "id": pir_id}


# ---------- dashboard widget ----------


class DashboardOverviewItem(BaseModel):
    pir_id: str
    title: str
    enabled: bool
    match_count_7d: int
    match_count_30d: int
    last_match_at: str | None
    sparkline_7d: list[int]  # 7 日分の daily count (Phase 1 は zero-padding で OK)
    # dashboard 最新ヘッドライン widget (PIR 軸) 用。compute_pir_kpi の評価結果を
    # 再利用するため追加コストなし (sample_limit を 0→N にするだけ)。
    headlines: list[PreviewMatchSample] = Field(default_factory=list)


class DashboardOverviewResponse(BaseModel):
    items: list[DashboardOverviewItem]


# dashboard widget は 60s 毎に再取得するため、短 TTL の process-level キャッシュで
# 反復コストを排除する。PIR 編集後は _invalidate_pir_caches() で即時反映する。
_OVERVIEW_CACHE: dict[str, tuple[float, DashboardOverviewResponse]] = {}
_OVERVIEW_TTL_SEC = 45.0


# 一覧と同じく同期関数 (async だと走査中に event loop を占有し UI 全体が固まる)。
@pir_api.get("/dashboard/overview", response_model=DashboardOverviewResponse)
def dashboard_overview() -> DashboardOverviewResponse:
    """Dashboard PIR widget 用の compact summary (短 TTL キャッシュ付き)。"""
    cached = _OVERVIEW_CACHE.get("overview")
    if cached is not None and (time.monotonic() - cached[0]) < _OVERVIEW_TTL_SEC:
        return cached[1]
    cfg = load_current_pir_config()
    enabled = [p for p in cfg.priorities if p.enabled]
    # 一覧と同様、PIR ごとの全走査ではなく 1 回の読込でまとめて評価する。
    # ここで要るのは件数・最終マッチ・見出し 3 件だけで、上位フィード/アクターは使わない。
    matches = evaluate_pirs_batch(enabled, lookback_hours=24 * 30, limit=15000)
    cutoff_7d = datetime.now(UTC) - timedelta(days=7)
    items: list[DashboardOverviewItem] = []
    for p in enabled:
        found = matches.get(p.id, [])
        count_7d = sum(1 for m in found if (_parse_dt(m.created_at) or cutoff_7d) >= cutoff_7d)
        # sparkline は Phase 1 では match_count_30d の単純分散で代用
        # (将来 pir_kpi_daily テーブルを作って正確に時系列化)
        items.append(
            DashboardOverviewItem(
                pir_id=p.id,
                title=p.title,
                enabled=p.enabled,
                match_count_7d=count_7d,
                match_count_30d=len(found),
                last_match_at=found[0].created_at if found else None,
                sparkline_7d=_placeholder_sparkline(count_7d),
                headlines=[_match_to_sample(m) for m in found[:3]],
            )
        )
    resp = DashboardOverviewResponse(items=items)
    _OVERVIEW_CACHE["overview"] = (time.monotonic(), resp)
    return resp


# ---------- 自動 PIR 提案 (Shadow F5) ----------


class SuggestedPirItem(BaseModel):
    title: str
    description: str
    rationale: str
    sample_articles: list[dict[str, Any]]


@pir_api.get("/suggestions", response_model=list[SuggestedPirItem])
async def list_pir_suggestions() -> list[SuggestedPirItem]:
    """Shadow F5: 自動 PIR 提案 (現状は placeholder、Phase 3 で実装)。"""
    return []


# ---------- internals ----------


def _slugify_id(title: str) -> str:
    """PIR id を title から生成 (新規作成時のデフォルト)。"""
    import re

    base = re.sub(r"[^a-zA-Z0-9_]", "_", title.lower()).strip("_")
    base = re.sub(r"_+", "_", base)
    if not base:
        base = "pir_new"
    return f"pir_{base}"[:64]


def _placeholder_sparkline(count_7d: int) -> list[int]:
    """7 日分の placeholder sparkline。

    Phase 3 で pir_kpi_daily table を実装したら実値に置換。
    """
    if count_7d == 0:
        return [0] * 7
    avg = count_7d // 7
    rest = count_7d - avg * 7
    return [avg + (1 if i < rest else 0) for i in range(7)]
