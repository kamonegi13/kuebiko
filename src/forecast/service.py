"""Phase 4: forecasting のビジネスロジック (storage + analytics の結線)。

- ``build_forecast``: read API 用に spike (FC3) / トレンド (FC4) / 相関 (FC5) と
  指標的中率 (FC2) を組み立てる。
- ``snapshot_and_verify_indicators``: 週次 synthesis 後に、前期 indicator を検証し
  当期の spike を新規 indicator として記録する accountability ループ (FC2)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.forecast import analytics
from src.forecast.models import (
    CorrelationPair,
    EntityTrend,
    ForecastIndicatorRecord,
    ForecastResult,
    IndicatorHitStats,
    IndicatorVerdictStatsV2,
)
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

DEFAULT_WEEKS = 8
DEFAULT_TOP_ACTORS = 12
DEFAULT_TOP_INTENTS = 8
# 相関 (FC5) で採用する最小 |r| と、各 entity の最小総活動 (spurious 排除)。
CORR_THRESHOLD = 0.6
CORR_MIN_TOTAL = 3
CORR_TOP = 6
# 指標的中率の集計窓 (週)。
HIT_STATS_LOOKBACK_WEEKS = 12
# FC2 v2 較正判定 (監査 2026-07-16 設計 §4b): Poisson kσ バンドと低頻度フロア。
V2_SIGMA_K = 2.0  # 偶然 hit 率 ≈2-3% に揃える (較正が外れたらここ 1 定数で調整)
V2_MIN_HIT_RATE = 2.0  # baseline < 1 の指標は週 2 件以上の観測で hit (フロア)
_SECONDS_PER_WEEK = 7 * 86400.0


def indicator_verdict_v2(
    *,
    baseline_avg: float,
    observed_count: int,
    period_start: datetime,
    verified_at: datetime,
) -> str:
    """FC2 v2 の較正判定 (hit / partial / miss、決定論)。

    v1 (観測累積 >= baseline) は spike 由来指標でほぼ常に hit になる準トートロジー
    だった。v2 は (a) 検証窓の週数で観測を正規化し (窓跨ぎの累積インフレ除去)、
    (b) Poisson 2σ バンドで「有意な増勢 (hit)」「基礎率での継続 (partial)」
    「下振れ・沈静化 (miss)」を分ける。閾値は baseline 依存で自動調整され、
    偶然 hit 率が全指標で概ね揃う。
    """
    weeks = max(1.0, (verified_at - period_start).total_seconds() / _SECONDS_PER_WEEK)
    rate = observed_count / weeks
    lam = max(0.0, baseline_avg)
    band = V2_SIGMA_K * (lam**0.5)
    hi = max(V2_MIN_HIT_RATE, lam + band)
    lo = max(0.0, lam - band)
    if rate >= hi:
        return "hit"
    if observed_count == 0 or rate < lo:
        return "miss"
    return "partial"


def _aware(dt: datetime) -> datetime:
    """naive datetime を UTC とみなして aware 化する (backend 差の防御)。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _build_trends(
    scope: str,
    times_by_value: dict[str, list[datetime]],
    *,
    weeks: int,
    now: datetime,
    top_n: int,
) -> list[EntityTrend]:
    """value → 出現時刻 から EntityTrend (週次 + trend + spike) を作る。"""
    trends: list[EntityTrend] = []
    for value, times in times_by_value.items():
        weekly = analytics.bucket_weekly(times, weeks=weeks, now=now)
        total = sum(weekly)
        if total == 0:
            continue
        direction, slope = analytics.trend_direction(weekly)
        z, is_spike = analytics.spike_score(weekly)
        trends.append(
            EntityTrend(
                scope=scope,  # type: ignore[arg-type]
                value=value,
                weekly=weekly,
                total=total,
                direction=direction,
                slope=slope,
                z_score=z,
                is_spike=is_spike,
            )
        )
    trends.sort(key=lambda t: t.total, reverse=True)
    return trends[:top_n]


def _load_enabled_pir_actor_names() -> list[tuple[str, str]]:
    """enabled PIR の actor signal を ``[(pir_id, actor_name_low), ...]`` で返す。

    PIR システム障害時は空 (= 既存の PIR 非依存挙動)。
    """
    try:
        from src.pir.integration import get_pir_config

        out: list[tuple[str, str]] = []
        for pir in get_pir_config().priorities:
            if not pir.enabled:
                continue
            for name in pir.strong_signals.actors:
                nl = str(name).lower().strip()
                if len(nl) >= 3:
                    out.append((pir.id, nl))
        return out
    except Exception as e:  # noqa: BLE001 — PIR 障害で forecast を壊さない
        _log.warning("forecast_pir_names_load_failed", error=str(e))
        return []


def _match_pirs_for_value(value: str, pir_names: list[tuple[str, str]]) -> list[str]:
    """forecast の actor value (entity 名) を PIR actor 名と双方向 word-boundary 照合。

    ③ Threats と同一の境界規則 (``_alias_pattern``) で "Lazarus"⇄"Lazarus Group" を
    一致させつつ "Grumman"→GRU 型の誤帰属を防ぐ。
    """
    vlow = (value or "").lower().strip()
    if not vlow or not pir_names:
        return []
    from src.cti.actor_normalizer import _alias_pattern

    hits: set[str] = set()
    for pir_id, nl in pir_names:
        if (nl in vlow and _alias_pattern(nl).search(vlow)) or (
            vlow in nl and _alias_pattern(vlow).search(nl)
        ):
            hits.add(pir_id)
    return sorted(hits)


def _annotate_trends_with_pir(
    trends: list[EntityTrend],
    pir_names: list[tuple[str, str]],
) -> list[EntityTrend]:
    """actor trend に matched_pir_ids を付与した新 list を返す (immutable copy)。"""
    if not pir_names:
        return trends
    return [
        t.model_copy(update={"matched_pir_ids": _match_pirs_for_value(t.value, pir_names)})
        for t in trends
    ]


def _correlations(trends: list[EntityTrend]) -> list[CorrelationPair]:
    """top trend 群の週次系列から強い相関ペアを抽出する (FC5)。"""
    eligible = [t for t in trends if t.total >= CORR_MIN_TOTAL]
    pairs: list[CorrelationPair] = []
    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            a, b = eligible[i], eligible[j]
            r = analytics.pearson(
                [float(x) for x in a.weekly],
                [float(x) for x in b.weekly],
            )
            if abs(r) >= CORR_THRESHOLD:
                pairs.append(
                    CorrelationPair(scope=a.scope, a=a.value, b=b.value, correlation=round(r, 3))
                )
    pairs.sort(key=lambda p: abs(p.correlation), reverse=True)
    return pairs[:CORR_TOP]


def build_forecast(
    repo: RunHistoryRepository,
    *,
    weeks: int = DEFAULT_WEEKS,
    now: datetime | None = None,
    period_type: str = "weekly",
) -> ForecastResult:
    """spike / トレンド / 相関 / 指標的中率を組み立てて返す (read API 用)。"""
    now = now or datetime.now(UTC)
    since = now - timedelta(weeks=weeks)

    actor_times = repo.entity_event_times("actor", since=since)
    intent_times = repo.intent_event_times(since=since)

    actor_trends = _build_trends(
        "actor", actor_times, weeks=weeks, now=now, top_n=DEFAULT_TOP_ACTORS
    )
    intent_trends = _build_trends(
        "intent", intent_times, weeks=weeks, now=now, top_n=DEFAULT_TOP_INTENTS
    )

    # ④ PIR 連携: actor trend に matched_pir_ids を付与 (intent は PIR actor 対象外)。
    pir_names = _load_enabled_pir_actor_names()
    actor_trends = _annotate_trends_with_pir(actor_trends, pir_names)

    # spike は PIR 該当を優先 (= 自分の重視脅威の予兆を上位に)、次に z_score 降順。
    spike_alerts = sorted(
        [t for t in (actor_trends + intent_trends) if t.is_spike],
        key=lambda t: (bool(t.matched_pir_ids), t.z_score),
        reverse=True,
    )
    correlations = _correlations(actor_trends)

    # FC2: 指標的中率 + 現行監視中の指標
    hit_since = now - timedelta(weeks=HIT_STATS_LOOKBACK_WEEKS)
    verified, hits = repo.forecast_indicator_hit_stats(period_type=period_type, since=hit_since)
    hit_rate = (hits / verified) if verified else 0.0
    open_indicators = repo.list_forecast_indicators(
        period_type=period_type, only_open=True, limit=50
    )
    # FC2 v2: 検証済み行から read-time に較正判定 (書き換えなし = v1 履歴を保つ)
    v2_counts: dict[str, int] = {"hit": 0, "partial": 0, "miss": 0}
    for ind in repo.list_forecast_indicators(period_type=period_type, limit=200):
        if ind.verified_at is None or _aware(ind.verified_at) < hit_since:
            continue
        verdict = indicator_verdict_v2(
            baseline_avg=ind.baseline_avg,
            observed_count=ind.observed_count,
            period_start=_aware(ind.period_start),
            verified_at=_aware(ind.verified_at),
        )
        v2_counts[verdict] += 1
    v2_total = sum(v2_counts.values())
    indicator_stats_v2 = IndicatorVerdictStatsV2(
        verified=v2_total,
        hits=v2_counts["hit"],
        partials=v2_counts["partial"],
        misses=v2_counts["miss"],
        hit_rate=round(v2_counts["hit"] / v2_total, 3) if v2_total else 0.0,
    )

    has_data = bool(actor_trends or intent_trends)
    return ForecastResult(
        generated_at=now,
        weeks=weeks,
        has_data=has_data,
        spike_alerts=spike_alerts,
        actor_trends=actor_trends,
        intent_trends=intent_trends,
        correlations=correlations,
        indicator_stats=IndicatorHitStats(
            verified=verified, hits=hits, hit_rate=round(hit_rate, 3)
        ),
        indicator_stats_v2=indicator_stats_v2,
        open_indicators=open_indicators,
    )


def _current_week_start(now: datetime) -> datetime:
    """now を含む週の月曜 00:00 UTC を返す (forecast の週次バケット境界)。"""
    midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday())


def _baseline_avg(weekly: list[int]) -> float:
    """直近週を除く baseline の平均活動。"""
    base = weekly[:-1]
    return (sum(base) / len(base)) if base else 0.0


def snapshot_and_verify_indicators(
    repo: RunHistoryRepository,
    *,
    period_type: str = "weekly",
    weeks: int = DEFAULT_WEEKS,
    now: datetime | None = None,
) -> tuple[int, int]:
    """FC2 accountability ループ: 前期 indicator を検証し、当期 spike を記録する。

    1. ``period_start`` (当週月曜) より前の未検証 indicator を、予測時点以降の実活動で
       検証する (hit = 予測後の活動が baseline 以上を維持)。
    2. 当期の spike を新規 ``rising`` indicator として upsert。

    戻り値: (snapshotted, verified)。
    """
    now = now or datetime.now(UTC)
    period_start = _current_week_start(now)

    # ----- 1. 前期 indicator の検証 -----
    open_prior = repo.list_open_forecast_indicators_before(
        period_type=period_type, before=period_start
    )
    verified = 0
    if open_prior:
        # scope ごとに event times を 1 回ずつ取得 (最古 period_start から)
        scopes = {ind.scope for ind in open_prior}
        times_cache: dict[str, dict[str, list[datetime]]] = {}
        min_start = min(ind.period_start for ind in open_prior)
        for scope in scopes:
            if scope == "intent":
                times_cache[scope] = repo.intent_event_times(since=min_start)
            else:
                times_cache[scope] = repo.entity_event_times(scope, since=min_start)
        for ind in open_prior:
            if ind.id is None:
                continue
            times = times_cache.get(ind.scope, {}).get(ind.target_value, [])
            observed = sum(1 for t in times if ind.period_start <= t <= now)
            threshold = max(1.0, ind.baseline_avg)
            hit = observed >= threshold
            repo.mark_forecast_indicator_verified(
                ind.id, hit=hit, observed_count=observed, when=now
            )
            verified += 1

    # ----- 2. 当期 spike を indicator として記録 -----
    forecast = build_forecast(repo, weeks=weeks, now=now, period_type=period_type)
    snapshotted = 0
    for trend in forecast.spike_alerts:
        latest = trend.weekly[-1] if trend.weekly else 0
        rec = ForecastIndicatorRecord(
            period_type=period_type,
            period_start=period_start,
            scope=trend.scope,
            target_value=trend.value,
            direction="rising",
            z_score=round(trend.z_score, 3),
            baseline_avg=round(_baseline_avg(trend.weekly), 3),
            latest_count=latest,
            rationale=f"spike z={trend.z_score:.1f} (直近 {latest} 件)",
        )
        repo.upsert_forecast_indicator(rec)
        snapshotted += 1

    _log.info(
        "forecast_indicators_snapshot",
        period_type=period_type,
        snapshotted=snapshotted,
        verified=verified,
    )
    return snapshotted, verified
