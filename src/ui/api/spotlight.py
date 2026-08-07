"""PIR Spotlight API (Phase Diamond verify-spotlight)。

GET / POST endpoints:
  GET  /api/v1/spotlight              全 enabled PIR の最新 Spotlight list
  GET  /api/v1/spotlight/{pir_id}     1 PIR の最新 Spotlight (詳細)
  POST /api/v1/spotlight/{pir_id}/regenerate  user 手動 regenerate (主に LLM 比較用)

POST は READ_ONLY middleware で 403 block (readonly instance では generation 不可)。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.config_loader import load_app_config
from src.cti.source_basis import compute_source_basis
from src.logging_config import get_logger
from src.pir.loader import load_pir_config
from src.spotlight.generator import generate_spotlight
from src.spotlight.models import KeyEvent, SpotlightPeriod, SpotlightRecord
from src.storage.run_history import RunHistoryRepository
from src.tools.kev_client import get_kev_cve_set
from src.tools.llm_client import LLMClient, LLMForbiddenModelError, OllamaClient
from src.tools.model_tiers import STEP_REGISTRY, Step, build_llm_for

_log = get_logger(__name__)

spotlight_api = APIRouter(prefix="/api/v1/spotlight", tags=["spotlight"])

# period_type (生 enum) → UI 表示用の日本語ラベル。
_PERIOD_LABELS: dict[str, str] = {"daily": "日次", "weekly": "週次", "monthly": "月次"}


class SourceBasisView(BaseModel):
    """S1: key event の出典基盤 (信頼度を source メタから決定的に算出)。"""

    confidence: str  # high / medium / low
    best_tier: str  # official / research / news / social / unknown
    source_count: int
    has_official_authority: bool
    social_only: bool
    reason: str


class KeyEventView(KeyEvent):
    """KeyEvent + 出典基盤バッジ (UI/Discord 表示用)。"""

    source_basis: SourceBasisView


class SpotlightSummary(BaseModel):
    """1 件の Spotlight (一覧 + 詳細表示共通)。"""

    pir_id: str
    pir_title: str
    period_type: str
    period_start: str  # ISO
    period_end: str
    headline: str
    outlook: str
    key_events: list[KeyEventView]
    article_count: int
    llm_model: str
    generated_at: str


def _enrich_event(
    ev: KeyEvent, repo: RunHistoryRepository, kev_set: frozenset[str]
) -> KeyEventView:
    # S4: key_event は単一 article のため、共有 CVE/actor を持つ別 source を逆引きして
    # 裏取りを機械的に検証する (14 日窓)。「Grok 単独だが同 CVE を CISA が報告」等を検出。
    sb = compute_source_basis(repo, [ev.article_id], kev_set=kev_set, corroborate_window_days=14)
    return KeyEventView(
        **ev.model_dump(),
        source_basis=SourceBasisView(
            confidence=sb.confidence,
            best_tier=sb.best_tier,
            source_count=sb.source_count,
            has_official_authority=sb.has_official_authority,
            social_only=sb.social_only,
            reason=sb.reason,
        ),
    )


def _record_to_summary(
    rec: SpotlightRecord, repo: RunHistoryRepository, kev_set: frozenset[str]
) -> SpotlightSummary:
    return SpotlightSummary(
        pir_id=rec.pir_id,
        pir_title=rec.pir_title,
        period_type=rec.period_type,
        period_start=rec.period_start.isoformat(),
        period_end=rec.period_end.isoformat(),
        headline=rec.headline,
        outlook=rec.outlook,
        key_events=[_enrich_event(ev, repo, kev_set) for ev in rec.key_events],
        article_count=rec.article_count,
        llm_model=rec.llm_model,
        generated_at=rec.generated_at.isoformat(),
    )


class SpotlightListResponse(BaseModel):
    period_type: str
    items: list[SpotlightSummary]


@spotlight_api.get("", response_model=SpotlightListResponse)
async def list_spotlights(period_type: SpotlightPeriod = "weekly") -> SpotlightListResponse:
    """全 PIR の最新 Spotlight を取得 (UI 一覧用)。"""
    repo = RunHistoryRepository()
    kev_set = get_kev_cve_set()
    records = repo.list_all_latest_spotlights(period_type=period_type)
    return SpotlightListResponse(
        period_type=period_type,
        items=[_record_to_summary(r, repo, kev_set) for r in records],
    )


@spotlight_api.get("/{pir_id}", response_model=SpotlightSummary)
async def get_spotlight(pir_id: str, period_type: SpotlightPeriod = "weekly") -> SpotlightSummary:
    """個別 PIR の最新 Spotlight (詳細画面用)。"""
    repo = RunHistoryRepository()
    rec = repo.get_latest_spotlight(pir_id=pir_id, period_type=period_type)
    if rec is None:
        raise HTTPException(
            status_code=404, detail="この PIR のスポットライトはまだ生成されていません"
        )
    return _record_to_summary(rec, repo, get_kev_cve_set())


class RegenerateRequest(BaseModel):
    period_type: SpotlightPeriod = "weekly"
    # LLM model override (None なら .env の OLLAMA_SPOTLIGHT_MODEL or MAIN_MODEL)
    model: str | None = None


@spotlight_api.post("/{pir_id}/regenerate", response_model=SpotlightSummary)
async def regenerate_spotlight(pir_id: str, req: RegenerateRequest) -> SpotlightSummary:
    """1 PIR の Spotlight を即時 regenerate (主に LLM 比較 / debug 用)。"""
    pir_cfg = load_pir_config()
    pir = pir_cfg.find(pir_id)
    if pir is None:
        raise HTTPException(status_code=404, detail=f"PIR が見つかりません: {pir_id}")

    app_cfg = load_app_config()
    # 既定は PIR spotlight ティア (fast)。req.model は A/B 比較用の明示 override
    # (compare_spotlight_models.py 経由)。override も whitelist を必ず通す (§4 防御)。
    if req.model:
        try:
            # 両分岐 (明示 override / ティア既定) が満たす共通インタフェースで受ける。
            # else 側の build_llm_for は LLMClient を返すため OllamaClient 注釈だと不一致。
            llm: LLMClient = OllamaClient(
                base_url=app_cfg.ollama_base_url,
                model=req.model,
                timeout_seconds=STEP_REGISTRY[Step.PIR_SPOTLIGHT].timeout_seconds,
            )
        except LLMForbiddenModelError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    else:
        llm = build_llm_for(Step.PIR_SPOTLIGHT, app_cfg)
    record = await generate_spotlight(pir, llm=llm, period_type=req.period_type)
    if record is None:
        period_label = _PERIOD_LABELS.get(req.period_type, req.period_type)
        raise HTTPException(
            status_code=400,
            detail=f"対象記事が無いか生成に失敗しました ({period_label})",
        )
    repo = RunHistoryRepository()
    repo.upsert_pir_spotlight(record)
    return _record_to_summary(record, repo, get_kev_cve_set())
