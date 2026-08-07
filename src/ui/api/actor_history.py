"""アクター行動史 (actor_observed_profile) + actor→situation 逆引きの read-only API。

アクター辞書 Phase1 (2026-07-26): 恒久史 (月次タイムライン) は辞書詳細、直近 90 日
精密観測は脅威ページ、という表示棲み分けの「辞書側」データ供給。
- 行動史は **収集網の観測の記録** であり世界の全体像ではない (観測≠世界) — note で明示
- merge 後の合算は表示時: resolve_actor_id() + merged_sources() で旧 id 行を月毎に加算
- GET のみのため readonly instance でも追加ガードなしで動く
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from src.cti.actor_normalizer import load_actor_aliases
from src.cti.actor_observed_history import (
    SUBJECT_EPOCH_MONTH,
    ActorMonthProfile,
    month_label,
    months_between,
)
from src.storage.run_history import RunHistoryRepository

actor_history_api = APIRouter(prefix="/api/v1/actors", tags=["actor-history"])

# 観測≠世界 doctrine の時間版 (UI がそのまま注記として表示する)
OBSERVATION_NOTE = "収集網が観測した subject 記事の記録であり、アクター活動の全体像ではない"


def _merge_counts(rows: list[dict[str, int]]) -> dict[str, int]:
    total: Counter[str] = Counter()
    for r in rows:
        total.update(r)
    return dict(total)


def _merge_month_rows(profiles: list[ActorMonthProfile]) -> list[dict[str, Any]]:
    """同一月の複数 id 行 (canonical + merge 旧 id) を表示用に合算する。"""
    by_month: dict[str, list[ActorMonthProfile]] = {}
    for p in profiles:
        by_month.setdefault(p.month, []).append(p)
    out: list[dict[str, Any]] = []
    for month in sorted(by_month):
        rows = by_month[month]
        out.append(
            {
                "month": month,
                "subject_articles": sum(r.subject_articles for r in rows),
                # 旧 id と canonical のソース集合は月次行では復元不能なため max で近似
                # (合算月は稀 — merge 直後の遷移月のみ)
                "distinct_sources": max((r.distinct_sources for r in rows), default=0),
                "sectors": _merge_counts([r.sectors for r in rows]),
                "countries": _merge_counts([r.countries for r in rows]),
                "malware": _merge_counts([r.malware for r in rows]),
                "ttps": _merge_counts([r.ttps for r in rows]),
                "campaigns": _merge_counts([r.campaigns for r in rows]),
                "japan_targeted": sum(r.japan_targeted for r in rows),
                "kev_hits": sum(r.kev_hits for r in rows),
            }
        )
    return out


@actor_history_api.get("/observed-summary")
def actors_observed_summary() -> dict[str, Any]:
    """一覧用バッチ (P2-S8): 各アクターの最終観測月・主題累計・照合実績のある名前。

    辞書一覧の鮮度列 (休眠エントリの棚卸し) と保守列 (死に alias 候補) の供給。
    per-actor endpoint の N+1 呼出を避けるため 2 クエリで全件返す。
    """
    repo = RunHistoryRepository()
    profiles = repo.actor_profile_summaries()
    usage_names = repo.alias_usage_names_by_actor()
    summaries: dict[str, dict[str, Any]] = {}
    for actor_id in set(profiles) | set(usage_names):
        prof = profiles.get(actor_id, {})
        summaries[actor_id] = {
            "last_month": prof.get("last_month"),
            "subject_total": prof.get("subject_total", 0),
            "matched_names": sorted(usage_names.get(actor_id, [])),
        }
    return {"summaries": summaries}


@actor_history_api.get("/{actor_id}/history")
def actor_history(actor_id: str) -> dict[str, Any]:
    """アクターの月次行動史 (epoch 以降、ゼロ月込みの連続 series 付き)。"""
    registry = load_actor_aliases()
    canonical = registry.resolve_actor_id(actor_id)
    ids = [canonical, *registry.merged_sources(canonical)]
    repo = RunHistoryRepository()
    months = _merge_month_rows(repo.list_actor_month_profiles(ids))
    # sparkline 用: epoch〜当月の連続系列 (観測なし月 = 0 を正直に示す)
    current = month_label(datetime.now(UTC))
    by_month = {m["month"]: int(m["subject_articles"]) for m in months}
    series = [
        {"month": m, "subject_articles": by_month.get(m, 0)}
        for m in months_between(SUBJECT_EPOCH_MONTH, current)
    ]
    return {
        "actor_id": canonical,
        "requested_id": actor_id,
        "merged_from": ids[1:],
        "epoch_month": SUBJECT_EPOCH_MONTH,
        "months": months,
        "series": series,
        # F5: 名前 (canonical/alias) ごとの累計ヒット記事数 — 死に alias の整理判断材料
        "alias_usage": repo.alias_usage_totals(ids),
        "note": OBSERVATION_NOTE,
    }


_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


@actor_history_api.get("/{actor_id}/history/{month}/articles")
def actor_month_articles(actor_id: str, month: str) -> dict[str, Any]:
    """月次行の証拠開示 (D5): その月にカウントされた主題記事のライブ照会。

    月次行 (集計) が永久保証、このリストは記事メタが現存する限りの便宜機能 —
    article_id を月次行に焼き込まない (死にリンクの永久固定と二重管理を避ける)。
    蒸留は週次のため集計件数と一時的にズレることがある (UI 側が注記)。
    """
    if not _MONTH_RE.match(month):
        raise HTTPException(status_code=400, detail="month は YYYY-MM 形式で指定してください")
    from src.cti.actor_observed_history import _dedupe_by_article, month_window_utc

    registry = load_actor_aliases()
    canonical = registry.resolve_actor_id(actor_id)
    ids = {canonical, *registry.merged_sources(canonical)}
    repo = RunHistoryRepository()
    since, before = month_window_utc(month)
    rows = [
        r
        for r in _dedupe_by_article(repo.list_subject_article_rows(since, before))
        if ids & {s.strip() for s in str(r["subject_actor_ids"]).split(",")}
    ]
    rows.sort(key=lambda r: str(r["created_at"]), reverse=True)
    total = len(rows)
    rows = rows[:100]
    # KEV 判定 (月行の kev_hits と同じ突合)
    try:
        from src.tools.kev_client import get_kev_cve_set

        kev = get_kev_cve_set()
    except Exception:  # noqa: BLE001 — KEV 欠落はバッジが消えるだけ
        kev = frozenset()
    entities = repo.list_entity_pairs_for_articles([str(r["article_id"]) for r in rows], ["cve"])
    from src.cti.japan_relevance import is_japan_targeted_row

    articles = [
        {
            "article_id": str(r["article_id"]),
            "title": str(r["title"] or ""),
            "url": str(r["url"] or ""),
            "feed_title": r["feed_title"],
            "importance": r["importance"],
            "created_at": str(r["created_at"]),
            "japan_targeted": is_japan_targeted_row(
                r["victim_country_iso"], str(r["posted_channel"] or "")
            ),
            "kev_hit": bool(
                {v.upper() for t, v in entities.get(str(r["article_id"]), ()) if t == "cve"}
                & {str(c).upper() for c in kev}
            ),
        }
        for r in rows
    ]
    return {"actor_id": canonical, "month": month, "articles": articles, "total": total}


@actor_history_api.get("/{actor_id}/situations")
def actor_situations(actor_id: str) -> dict[str, Any]:
    """actor に anchor された状況台帳 situation の逆引き (F2)。"""
    from src.assessment.situation_store import SituationStore

    registry = load_actor_aliases()
    canonical = registry.resolve_actor_id(actor_id)
    keys = {f"actor:{i}" for i in (canonical, *registry.merged_sources(canonical))}
    store = SituationStore()
    rows = store.load_situations(("active", "dormant", "closed"))
    items = [
        {
            "situation_id": r.situation_id,
            "title": r.title,
            "domain": r.domain,
            "status": r.status,
            "kind": r.kind,
            "opened_at": r.opened_at,
            "last_evidence_at": r.last_evidence_at,
        }
        for r in rows
        if keys & set(r.anchors)
    ]
    items.sort(key=lambda x: str(x["last_evidence_at"] or ""), reverse=True)
    return {"actor_id": canonical, "situations": items, "total": len(items)}
