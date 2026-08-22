"""I&W (Indications & Warning) 日次バースト検出。

既存の forecast サブシステム (週次 FC2〜FC5、``analytics.py`` / ``service.py``) の
**日次拡張**。週次 spike は「先週との比較」で 1 週間遅れの検知になるため、
actor (言及ストリーム) / sector / country の当日活動を毎朝チェックし、統計的に
有意な急増 (I&W シグナル) を即日拾う。

設計は週次 FC2/FC3 と対称:
- FC3 相当: ``analytics.spike_score`` を日次バケットに適用 (z-score + min_count)。
- FC2 相当: 検出した burst を ``forecast_indicators`` (period_type='daily') に
  記録し、``VERIFY_AFTER_DAYS`` 後に close-out する accountability ループ。
- 継続バーストの毎朝連呼を防ぐため、同一 (scope, value) は ``SUPPRESSION_DAYS``
  以内に open な indicator があれば再起票しない。

この機能の失敗で朝ブリーフ全体を止めないよう、例外はすべて握って空/部分結果を返す。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from src.forecast import analytics
from src.forecast.analytics import DAYS_PER_WEEK
from src.forecast.models import ForecastIndicatorRecord
from src.forecast.service import _aware, _load_enabled_pir_actor_names, _match_pirs_for_value
from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

# baseline 窓 (日)。週次 (8 週=56 日) より短く、直近の「地」を見る。
DAILY_WINDOW_DAYS = 28
# 日次はノイズが大きいので週次 (2.0) より厳しい z 閾値。
DAILY_SPIKE_Z = 2.5
# 当日 3 件未満はバーストと呼ばない (0→1 のノイズ除け)。
DAILY_SPIKE_MIN_COUNT = 3
# 同一 (scope, value) の再起票抑制窓 (日)。継続バーストの毎朝連呼を防ぐ。
SUPPRESSION_DAYS = 7
# 日次 indicator の close-out までの日数 (FC2 accountability ループ)。
VERIFY_AFTER_DAYS = 7
# actor scope で母数にする entity 数上限 (entity_event_times と同じ考え方)。
_ACTOR_LIMIT_VALUES = 200
# format_burst_compact で列挙する上位件数。
_FORMAT_TOP_N = 5
# scope の日本語接頭 (UI 文言規約: 生 enum 直接表示禁止)。
_SCOPE_PREFIX_JA: dict[str, str] = {"actor": "アクター", "sector": "セクター", "country": "被害国"}

_EventStreams = dict[str, dict[str, list[datetime]]]


@dataclass(frozen=True)
class BurstEvent:
    """1 件の日次バースト検出結果 (immutable)。"""

    scope: str  # "actor" | "sector" | "country"
    value: str  # canonical id (actor entity 名 / sector canonical / ISO 国コード)
    label: str  # 日本語表示ラベル (生 enum 直接表示禁止の UI 規約)
    today_count: int
    baseline_daily_avg: float  # 当日を除く baseline の日次平均
    z_score: float
    matched_pir_ids: tuple[str, ...] = ()  # actor のみ


def _resolve_label(scope: str, value: str) -> str:
    """scope 別の表示ラベルを解決する (失敗時は値をそのまま返す = fail-open)。

    actor は entity 名をそのまま表示可能な固有名として使う。sector/country は
    geo_cyber_map の yaml 表示マップ (SSoT = config/cti/{victim_sectors,countries}.yaml)
    を再利用する (複製辞書を作らない — CLAUDE.md §7 規約)。
    """
    if scope == "actor":
        return value
    try:
        from src.ui.services.geo_cyber_map import (
            _COUNTRIES_YAML,
            _SECTORS_YAML,
            _yaml_display_map,
        )

        if scope == "sector":
            return _yaml_display_map(str(_SECTORS_YAML)).get(value, value)
        if scope == "country":
            return _yaml_display_map(str(_COUNTRIES_YAML)).get(value.upper(), value)
    except Exception as e:  # noqa: BLE001 — ラベル解決失敗で検出自体を壊さない
        _log.warning("burst_label_resolve_failed", scope=scope, value=value, error=str(e))
    return value


def _load_streams(repo: RunHistoryRepository, *, since: datetime) -> _EventStreams:
    """scope 別の value → 出現時刻 を集める。

    actor は言及ストリーム (entity_event_times、週次 FC3 と同じ源)。sector/country は
    ``victim_sector_event_times`` / ``victim_country_event_times`` (posted かつ
    cyber-attack category のみ、repo_synthesis.py 参照)。
    """
    return {
        "actor": repo.entity_event_times("actor", since=since, limit_values=_ACTOR_LIMIT_VALUES),
        "sector": repo.victim_sector_event_times(since=since),
        "country": repo.victim_country_event_times(since=since),
    }


def _find_spike_candidates(streams: _EventStreams, *, now: datetime) -> list[BurstEvent]:
    """streams の各 value を日次バケット化し、spike のみ BurstEvent 候補にする。"""
    candidates: list[BurstEvent] = []
    for scope, times_by_value in streams.items():
        for value, times in times_by_value.items():
            daily = analytics.bucket_daily(times, days=DAILY_WINDOW_DAYS, now=now)
            z, is_spike = analytics.spike_score(
                daily, min_count=DAILY_SPIKE_MIN_COUNT, z_threshold=DAILY_SPIKE_Z
            )
            if not is_spike:
                continue
            baseline = daily[:-1]
            baseline_avg = (sum(baseline) / len(baseline)) if baseline else 0.0
            candidates.append(
                BurstEvent(
                    scope=scope,
                    value=value,
                    label=_resolve_label(scope, value),
                    today_count=daily[-1] if daily else 0,
                    baseline_daily_avg=round(baseline_avg, 3),
                    z_score=round(z, 3),
                )
            )
    return candidates


def _annotate_actor_pir(candidates: list[BurstEvent]) -> list[BurstEvent]:
    """actor scope の候補に matched_pir_ids を付与した新 list を返す (immutable copy)。"""
    pir_names = _load_enabled_pir_actor_names()
    if not pir_names:
        return candidates
    out: list[BurstEvent] = []
    for c in candidates:
        if c.scope != "actor":
            out.append(c)
            continue
        matched = tuple(_match_pirs_for_value(c.value, pir_names))
        out.append(replace(c, matched_pir_ids=matched) if matched else c)
    return out


def _load_suppressed_keys(repo: RunHistoryRepository, *, now: datetime) -> set[tuple[str, str]]:
    """直近 ``SUPPRESSION_DAYS`` 以内に起票済み (open) の (scope, target_value) を返す。

    継続バーストが毎朝連呼されるのを防ぐ (同一対象が既に監視中なら再起票しない)。
    """
    cutoff = now - timedelta(days=SUPPRESSION_DAYS)
    open_daily = repo.list_forecast_indicators(period_type="daily", only_open=True, limit=200)
    return {
        (ind.scope, ind.target_value) for ind in open_daily if _aware(ind.period_start) >= cutoff
    }


def _day_start(now: datetime) -> datetime:
    """now を含む日の 00:00 UTC を返す (daily indicator の period_start 境界)。"""
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _persist_new_indicators(
    repo: RunHistoryRepository, candidates: list[BurstEvent], *, now: datetime
) -> None:
    """新規バースト候補を forecast_indicators (period_type='daily') に記録する。"""
    period_start = _day_start(now)
    for c in candidates:
        rec = ForecastIndicatorRecord(
            period_type="daily",
            period_start=period_start,
            scope=c.scope,  # type: ignore[arg-type]
            target_value=c.value,
            direction="rising",
            z_score=c.z_score,
            # baseline_avg は週換算 (日次平均 × 7) で保存する。indicator_verdict_v2
            # (FC2) は観測を検証窓の週数で正規化した rate と baseline_avg を比較する
            # ため、日次平均のまま入れると尺度が合わず (7 倍) 過小評価になる。
            baseline_avg=round(c.baseline_daily_avg * DAYS_PER_WEEK, 3),
            latest_count=c.today_count,
            rationale=f"日次バースト検出 (I&W): {c.label} z={c.z_score:.1f}",
        )
        repo.upsert_forecast_indicator(rec)


def _close_out_stale_indicators(
    repo: RunHistoryRepository, streams: _EventStreams, *, now: datetime
) -> None:
    """``VERIFY_AFTER_DAYS`` 経過した open な daily indicator を close-out する。

    観測窓は ``(period_start + 1d, period_start + VERIFY_AFTER_DAYS]`` (当日は除く)。
    判定式は週次
    ``snapshot_and_verify_indicators`` と同じ (baseline_avg は既に週換算済みなので
    そのまま閾値に使える)。``DAILY_WINDOW_DAYS`` より古い period_start は streams
    の fetch (``since``) より前になるため、その分の活動は数えられない — observed
    は 28 日窓内で拾えた分のみで close する (過小評価はあり得るが安全側)。
    ``period_type='daily'`` のみを対象にし、weekly の行には一切触れない。
    """
    cutoff = now - timedelta(days=VERIFY_AFTER_DAYS)
    stale = repo.list_open_forecast_indicators_before(period_type="daily", before=cutoff)
    for ind in stale:
        if ind.id is None:
            continue
        times = streams.get(ind.scope, {}).get(ind.target_value, [])
        start = _aware(ind.period_start)
        # 検証窓は **バースト当日を除いた** 翌日以降 (2026-08-22 修正)。当日を含めると
        # バースト当日の件数だけで閾値に届き、検定がトートロジー化する
        # (過去 90 日実測: hit 77.6% のうち 30.5% が当日だけで決着していた。
        # 当日除外後は hit 63.3% = 検出力のある検定になる)。
        window_start = start + timedelta(days=1)
        window_end = start + timedelta(days=VERIFY_AFTER_DAYS)
        # 境界は翌日 00:00 を **含む** (除くのはバースト当日だけ)。
        observed = sum(1 for t in times if window_start <= t <= window_end)
        threshold = max(1.0, ind.baseline_avg)
        repo.mark_forecast_indicator_verified(
            ind.id, hit=observed >= threshold, observed_count=observed, when=now
        )


def detect_daily_bursts(
    repo: RunHistoryRepository,
    *,
    now: datetime | None = None,
    persist: bool = True,
) -> list[BurstEvent]:
    """actor/sector/country の当日急増 (I&W シグナル) を検出する。

    ``persist=True`` (既定) では新規候補を daily indicator として記録し、期限到来した
    既存 daily indicator を close-out する。戻り値は今回「新規に」バーストと判定した
    ``BurstEvent`` (抑制でスキップした継続分は含めない)。PIR 該当 (actor) を先頭に、
    次に z_score 降順で並べる。
    """
    now = now or datetime.now(UTC)
    try:
        streams = _load_streams(repo, since=now - timedelta(days=DAILY_WINDOW_DAYS))
        candidates = _annotate_actor_pir(_find_spike_candidates(streams, now=now))
    except Exception as e:  # noqa: BLE001 — 検出失敗で朝ブリーフを止めない
        _log.warning("daily_burst_detection_failed", error=str(e))
        return []

    try:
        suppressed = _load_suppressed_keys(repo, now=now)
    except Exception as e:  # noqa: BLE001
        _log.warning("daily_burst_suppression_check_failed", error=str(e))
        suppressed = set()

    fresh = [c for c in candidates if (c.scope, c.value) not in suppressed]
    fresh.sort(key=lambda c: (bool(c.matched_pir_ids), c.z_score), reverse=True)

    if persist:
        try:
            _persist_new_indicators(repo, fresh, now=now)
        except Exception as e:  # noqa: BLE001
            _log.warning("daily_burst_persist_failed", error=str(e))
        try:
            _close_out_stale_indicators(repo, streams, now=now)
        except Exception as e:  # noqa: BLE001
            _log.warning("daily_burst_closeout_failed", error=str(e))

    return fresh


def format_burst_compact(events: Sequence[BurstEvent]) -> str:
    """日次バースト検出結果を Discord 投稿向けの短いテキストに整形する。

    空なら "" (節ごと省略)。記事本文・URL は含めない (機密マスク方針に抵触しない
    集計値のみ)。呼出側 (``detect_daily_bursts``) が既に優先順位で並べた ``events``
    の先頭 ``_FORMAT_TOP_N`` 件のみを列挙する。
    """
    if not events:
        return ""
    lines = ["📈 急増検知 (I&W) — 言及件数の統計。収集網の観測であり活動量そのものではない"]
    for e in events[:_FORMAT_TOP_N]:
        prefix = _SCOPE_PREFIX_JA.get(e.scope, e.scope)
        flag = " ⚑PIR" if e.scope == "actor" and e.matched_pir_ids else ""
        lines.append(
            f"・{prefix} {e.label}: 本日 {e.today_count} 件 "
            f"(基準 {e.baseline_daily_avg:.1f} 件/日, z={e.z_score:.1f}){flag}"
        )
    return "\n".join(lines)
