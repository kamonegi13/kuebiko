"""Intel Graph router (post-React-migration、redirects only)。

React SPA (/app/) が main UI。本 router は旧 URL からの redirect のみ提供。
JSON API は src/ui/api/v1.py + src/ui/api/pages.py。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from src.config_loader import load_app_config
from src.cti.taxonomy_normalizer import load_normalizer
from src.logging_config import get_logger
from src.ui.services.intel_graph_analytics import (
    fetch_axis_dashboard,
    resolve_incident_link,
)

_log = get_logger(__name__)

router = APIRouter()


# ---------- Helpers (shared with src/ui/api/v1.py) ----------


def _sector_display(_normalizer: object, canonical: str) -> str:
    if canonical == "uncategorized":
        return "(未分類)"
    return canonical


def _country_display(_normalizer: object, iso: str) -> str:
    return iso


def _spike_label(spike_ratio: float, is_new: bool) -> str:
    """平常比の急増度をそのまま表示できる日本語句にする。

    UI は絵文字を使わない (2026-06-13 に lucide へ統一) ため、強調は呼び出し側の
    スタイルに任せて文言は倍率のみを伝える。
    """
    if is_new or spike_ratio == float("inf"):
        return "初観測"
    if spike_ratio > 0:
        return f"平常比 {spike_ratio:.1f} 倍"
    return "—"


# 供給劣化判定 (監査 2026-07-16): baseline がこの件数以上あった軸が 20% 未満に
# 崩落したら「事象なし」でなく「供給劣化」として扱う (不明≠ゼロの正直表示)。
_DEGRADED_BASELINE_MIN = 20.0
_DEGRADED_RATIO = 0.2


def _build_pmesii_cards(
    *,
    lookback_hours: int,
    baseline_weeks: int,
    focused_axis: str = "",
) -> tuple[list[dict[str, Any]], int]:
    """PMESII card 集合と非空件数を返す (JSON API v1 経由で React に提供)。"""
    _ = focused_axis
    cards = fetch_axis_dashboard(
        lookback_hours=lookback_hours,
        baseline_weeks=baseline_weeks,
        recent_incident_limit=10,
    )
    cfg = load_app_config()
    guild_id = cfg.discord_guild_id or ""
    normalizer = load_normalizer()

    # 監査 backlog 2026-07-05: synthesis_events (axes_evidence 由来) は撤去。
    # grounded pipeline は axes_evidence="{}" を保存する (render.py: UI は estimate を
    # 見る設計) ため PMESII カードの synthesis_events は恒久空になっており、
    # frontend にも参照コンポーネントが存在しなかった (型定義のみ)。
    # estimate 射影への置換は judgment→PMESII 軸タグ付けの新設計が必要で、
    # 表示先が無い現状では YAGNI (必要になれば estimate 側から設計する)。

    cards_view: list[dict[str, Any]] = []
    non_empty = 0
    for c in cards:
        # 供給劣化の可視化 (監査 2026-07-16): baseline 比 <20% への崩落は「非空」に
        # 数えない (I-infra が 292→6 件/週に枯死しても non_empty に隠れていた対処)。
        is_degraded = (
            c.total_baseline_avg >= _DEGRADED_BASELINE_MIN
            and c.total_current < _DEGRADED_RATIO * c.total_baseline_avg
        )
        if c.total_current > 0 and not is_degraded:
            non_empty += 1
        cards_view.append(
            {
                "axis_id": c.axis_id,
                "display": c.display,
                "is_degraded": is_degraded,
                "total_current": c.total_current,
                "total_baseline_avg": f"{c.total_baseline_avg:.1f}",
                "spike_ratio_label": _spike_label(c.spike_ratio, c.is_new),
                "is_spike": c.is_spike,
                "is_new": c.is_new,
                "top_sectors": [
                    {"label": _sector_display(normalizer, s.label), "count": s.count}
                    for s in c.top_sectors
                ],
                "top_countries": [
                    {"label": _country_display(normalizer, co.label), "count": co.count}
                    for co in c.top_countries
                ],
                "recent_incidents": [
                    {
                        "title": i.title,
                        "feed_title": i.feed_title,
                        "importance": i.importance,
                        "url": resolve_incident_link(i, guild_id=guild_id),
                        "created_at": i.created_at[:10],
                    }
                    for i in c.recent_incidents
                ],
                "related_actors": 0,
            },
        )
    return cards_view, non_empty


# ---------- Legacy redirects → React SPA ----------


@router.get("/intel-graph")
async def legacy_root_redirect() -> Any:
    return RedirectResponse(url="/app/", status_code=302)


@router.get("/intel-graph/summary")
async def legacy_summary_redirect() -> Any:
    return RedirectResponse(url="/app/#tab=synthesis", status_code=302)


@router.get("/intel-graph/actors")
async def legacy_actors_redirect() -> Any:
    return RedirectResponse(url="/app/#tab=threats", status_code=302)


@router.get("/intel-graph/actor/{actor_id}")
async def legacy_actor_redirect(actor_id: str) -> Any:
    return RedirectResponse(url=f"/app/#tab=threats&actor={actor_id}", status_code=302)


@router.get("/intel-graph/taxonomy-review")
async def legacy_taxonomy_redirect() -> Any:
    return RedirectResponse(url="/app/intel/operations", status_code=302)


@router.get("/intel-graph/editorial-quality")
async def legacy_editorial_redirect() -> Any:
    return RedirectResponse(url="/app/intel/operations", status_code=302)
