"""Phase 6 (過去参照): time-machine 振り返り。

「あの週、何が起きていたか」を既存データ (synthesis 履歴 + 記事 + actor 活動 +
Phase 4 forecast の的中結果) から再構成する。Phase 5/6 の蓄積を待たずに、既に貯まった
週次 synthesis と記事で今すぐ価値が出る。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.storage.run_history import RunHistoryRepository

_JST = ZoneInfo("Asia/Tokyo")
_MAX_TOP_ARTICLES = 25
_MAX_TOP_ACTORS = 12


def _week_window(weeks_ago: int, now: datetime) -> tuple[datetime, datetime, str]:
    """``weeks_ago`` 週前の JST 月曜 00:00 を起点とする 1 週間 window を返す。"""
    base = now.astimezone(_JST)
    this_monday = base.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=base.weekday()
    )
    target = this_monday - timedelta(weeks=weeks_ago)
    start = target.astimezone(UTC)
    end = (target + timedelta(days=7)).astimezone(UTC)
    label = f"{target:%Y-%m-%d} 〜 {(target + timedelta(days=6)):%Y-%m-%d} JST"
    return start, end, label


def build_retrospect(
    repo: RunHistoryRepository,
    *,
    weeks_ago: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """指定週の振り返りスナップショットを組み立てる (read-only)。"""
    now = now or datetime.now(UTC)
    start, end, label = _week_window(weeks_ago, now)

    # 1. その週の weekly synthesis (period_start が window 内)
    synthesis: dict[str, Any] | None = None
    for rec in repo.list_synthesis(period_type="weekly", limit=26):
        if start <= rec.period_start < end:
            synthesis = {
                "headline": rec.headline,
                "weight_section": rec.weight_section,
                "chain_section": rec.chain_section,
                "cog_section": rec.cog_section,
                "spillover_section": rec.spillover_section,
                "pir_section": rec.pir_section,
                "article_count": rec.article_count,
                "generated_at": rec.generated_at.isoformat(),
            }
            break

    # 2. その週の主要記事 (high/medium、新しい順)
    arts = repo.list_articles(
        importance_in=["high", "medium"],
        status="posted",
        since=start,
        until=end,
        limit=_MAX_TOP_ARTICLES,
    )
    top_articles = [
        {
            "article_id": a.article_id,
            "title": a.title,
            "url": a.url,
            "importance": a.importance,
            "category": a.category,
            "socio_political_intent": a.socio_political_intent,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in arts
    ]

    # 3. その週に活動した actor (出現件数 top)
    actor_times = repo.entity_event_times("actor", since=start)
    actor_counts: list[tuple[str, int]] = []
    for value, times in actor_times.items():
        n = sum(1 for t in times if start <= t < end)
        if n > 0:
            actor_counts.append((value, n))
    actor_counts.sort(key=lambda kv: kv[1], reverse=True)
    top_actors = [{"value": v, "count": n} for v, n in actor_counts[:_MAX_TOP_ACTORS]]

    # 4. その週に立てた forecast 指標の的中結果 (Phase 4 FC2 retrospection)
    forecast_outcomes: list[dict[str, Any]] = []
    for ind in repo.list_forecast_indicators(period_type="weekly", limit=300):
        if start <= ind.period_start < end:
            forecast_outcomes.append(
                {
                    "scope": ind.scope,
                    "target_value": ind.target_value,
                    "direction": ind.direction,
                    "z_score": ind.z_score,
                    "verified": ind.verified_at is not None,
                    "hit": ind.hit,
                    "observed_count": ind.observed_count,
                }
            )

    # 5. その週の weekly recap 本文 (段5: 永続化した F1 deep dive narrative)
    recap = repo.get_weekly_recap_in_window(start=start, end=end)

    return {
        "weeks_ago": weeks_ago,
        "period": {"start": start.isoformat(), "end": end.isoformat(), "label": label},
        "synthesis": synthesis,
        "recap": recap,
        "top_articles": top_articles,
        "top_actors": top_actors,
        "forecast_outcomes": forecast_outcomes,
        "has_data": bool(synthesis or recap or top_articles or top_actors),
    }


def build_brief_context(
    repo: RunHistoryRepository,
    *,
    until: datetime,
) -> dict[str, Any]:
    """ブリーフ閲覧時の補足コンテキスト (時間軸統合 P2/P3、read-only)。

    日次ブリーフの本文 (配信物 = 不変の記録) には焼き込まず、閲覧時に計算する —
    振り返り (週次) と同じ「本文 + 活動アクター + 予測との照合」の文法を日次にも与える。

    - top_actors: ``until`` 直前 24h に活動した actor (週次集計と同じクエリの窓替え)
    - forecast_indicators: ``until`` が属する JST 週の weekly 予測指標。的中検証は
      週次のまま — 未検証は UI 側で「観測中」として表示する
    """
    start = until - timedelta(hours=24)
    actor_times = repo.entity_event_times("actor", since=start)
    actor_counts: list[tuple[str, int]] = []
    for value, times in actor_times.items():
        n = sum(1 for t in times if start <= t < until)
        if n > 0:
            actor_counts.append((value, n))
    actor_counts.sort(key=lambda kv: kv[1], reverse=True)
    top_actors = [{"value": v, "count": n} for v, n in actor_counts[:_MAX_TOP_ACTORS]]

    wk_start, wk_end, _wk_label = _week_window(0, until)
    forecast_indicators: list[dict[str, Any]] = []
    for ind in repo.list_forecast_indicators(period_type="weekly", limit=300):
        if wk_start <= ind.period_start < wk_end:
            forecast_indicators.append(
                {
                    "scope": ind.scope,
                    "target_value": ind.target_value,
                    "direction": ind.direction,
                    "verified": ind.verified_at is not None,
                    "hit": ind.hit,
                }
            )

    jst_start = start.astimezone(_JST)
    jst_until = until.astimezone(_JST)
    return {
        "window_label": f"{jst_start:%m/%d %H:%M} 〜 {jst_until:%m/%d %H:%M} JST",
        "top_actors": top_actors,
        "forecast_indicators": forecast_indicators,
    }
