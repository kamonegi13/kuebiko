"""週次 fill-rate 監査の節: alert の取り込みの遅延 p90 (計画 AT-7)。

遅延 = 取込時刻 (created_at) - 公開時刻 (published_at)。速報性の監視で、
目標 (p90 を何時間に置くか) は利用者判断待ちのため **WARN は出さず数値を出すだけ**。

時刻の意味論 (CLAUDE.md §7):
- 週の窓は **事象時刻** (``EVENT_TS_EXPR`` = 公開時刻、取込で上限、欠損は取込時刻) で切る。
  書いた時刻で切るとバックフィルが当日の週へ混ざる。
- 遅延そのものは生の ``published_at`` で測る (錨は取込で上限をかけるため、負の遅延が 0 に
  隠れて見えなくなる)。公開時刻が取込より後 (負) の行と公開時刻の無い行は百分位から外し、
  件数だけ別に出す。
- articles は同一 article_id が複数行ありうるため、article_id ごとに 1 行へ畳む。
- scraper は公開日 = 取込時刻になるため遅延は過小に出る (既知の偏り)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from src.storage.event_time import EVENT_TS_EXPR

ALERT_CHANNEL = "alert"
WINDOW_DAYS = 7
P90 = 0.9
MEDIAN = 0.5
SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True)
class AlertLatencyStats:
    n_total: int
    n_measured: int
    n_negative: int
    n_no_published: int
    median_hours: float | None
    p90_hours: float | None


def percentile(sorted_values: list[float], q: float) -> float | None:
    """線形補間の百分位 (PostgreSQL percentile_cont と同じ定義)。空は None。"""
    if not sorted_values:
        return None
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _parse(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def summarize_latency(pairs: list[tuple[Any, Any]]) -> AlertLatencyStats:
    """(created_at, published_at) の組から遅延の統計を作る。"""
    hours: list[float] = []
    negative = 0
    no_published = 0
    for created_raw, published_raw in pairs:
        created = _parse(created_raw)
        published = _parse(published_raw)
        if created is None or published is None:
            no_published += 1
            continue
        if (created.tzinfo is None) != (published.tzinfo is None):
            no_published += 1  # 時刻帯の有無が食い違う行は測れない
            continue
        delta = (created - published).total_seconds()
        if delta < 0:
            negative += 1
            continue
        hours.append(delta / SECONDS_PER_HOUR)
    hours.sort()
    return AlertLatencyStats(
        n_total=len(pairs),
        n_measured=len(hours),
        n_negative=negative,
        n_no_published=no_published,
        median_hours=percentile(hours, MEDIAN),
        p90_hours=percentile(hours, P90),
    )


def collect_alert_latency(repo: Any, *, until: datetime) -> AlertLatencyStats:
    """``until`` の直前 7 日 (事象時刻) に公開された alert 配信記事の遅延統計。"""
    since_iso = (until - timedelta(days=WINDOW_DAYS)).isoformat()
    until_iso = until.isoformat()
    ts = EVENT_TS_EXPR.format(a="a")
    sql = (
        "SELECT a.created_at AS created_at, a.published_at AS published_at FROM ("
        " SELECT article_id, MIN(created_at) AS created_at, MIN(published_at) AS published_at"
        " FROM articles WHERE posted_channel = ? GROUP BY article_id"
        f") a WHERE {ts} >= ? AND {ts} < ?"
    )
    with repo._connect() as conn:  # noqa: SLF001 — 監査の読み取り
        rows = conn.execute(sql, (ALERT_CHANNEL, since_iso, until_iso)).fetchall()
    return summarize_latency([(r["created_at"], r["published_at"]) for r in rows])


def alert_latency_line(stats: AlertLatencyStats) -> str:
    """週次報告の 1 行 (WARN なし。目標は利用者判断待ち)。"""
    if stats.p90_hours is None or stats.median_hours is None:
        return f"alert の取込遅延 (7日): 測れた件数 0 (alert {stats.n_total} 件)"
    return (
        f"alert の取込遅延 (7日): p90 {stats.p90_hours:.1f}h・中央値 {stats.median_hours:.1f}h"
        f" (n={stats.n_measured}/{stats.n_total}、公開時刻が取込より後 {stats.n_negative}"
        f"・公開時刻なし {stats.n_no_published} は除く)"
    )
