"""「成功なのに要約 0 件」が続く収集の検知 (2026-11-18)。

run は ``succeeded`` で、取得 (total_fetched) もあるのに、要約 (summarized) が 0 件の
まま続く — 例外も失敗通知も出ないため、利用者が気づくまで誰も知らない型の故障。
Grok は 9-11〜9-22 (12 日) と 11-15〜11-18 (4 日) の 2 回、この形で止まっていた。

判定 (結果を見る前に決めた値。過去 93 日の実行記録で 2 回の停止を両方拾い、他の収集の
誤報 0。24 時間の窓は 1 日早いが点滅し誤報 1 件のため採らない):
- 直近 ``WINDOW_HOURS`` の succeeded かつ取得ありの run が ``MIN_RUNS`` 回以上
- その要約の合計が 0
- その前の ``BASELINE_DAYS`` 日には要約が 1 件以上 (元から要約しない収集を除く)

日次ハートビートの 1 行として出す (``source_health.run_daily_heartbeat``)。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

WINDOW_HOURS = 48
MIN_RUNS = 3
BASELINE_DAYS = 14


@dataclass(frozen=True)
class RunTotals:
    pipeline: str
    started_at: datetime
    status: str
    total_fetched: int
    summarized: int


@dataclass(frozen=True)
class ZeroOutput:
    pipeline: str
    runs: int
    fetched: int


def detect_zero_output(runs: Iterable[RunTotals], *, now: datetime) -> list[ZeroOutput]:
    """窓の中で取得はあるのに要約 0 件が続く収集を返す (純粋関数)。"""
    window_start = now - timedelta(hours=WINDOW_HOURS)
    baseline_start = window_start - timedelta(days=BASELINE_DAYS)
    in_window: dict[str, list[RunTotals]] = defaultdict(list)
    baseline: dict[str, int] = defaultdict(int)
    for r in runs:
        if window_start <= r.started_at < now:
            if r.status == "succeeded" and r.total_fetched > 0:
                in_window[r.pipeline].append(r)
        elif baseline_start <= r.started_at < window_start:
            baseline[r.pipeline] += r.summarized
    out = [
        ZeroOutput(pipeline=p, runs=len(rs), fetched=sum(r.total_fetched for r in rs))
        for p, rs in in_window.items()
        if len(rs) >= MIN_RUNS and sum(r.summarized for r in rs) == 0 and baseline[p] > 0
    ]
    return sorted(out, key=lambda z: z.pipeline)


def build_zero_output_line(
    repo: RunHistoryRepository, *, now: datetime | None = None
) -> str | None:
    """ハートビートの 1 行。該当なしは「なし」、失敗は None (ハートビート本体は止めない)。"""
    try:
        at = now or datetime.now(UTC)
        since = (at - timedelta(hours=WINDOW_HOURS) - timedelta(days=BASELINE_DAYS)).isoformat()
        with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用
            rows = conn.execute(
                "SELECT pipeline, started_at, status, total_fetched, summarized"
                " FROM runs WHERE started_at >= ?",
                (since,),
            ).fetchall()
        runs = [
            RunTotals(
                pipeline=str(r["pipeline"]),
                started_at=_as_utc(r["started_at"]),
                status=str(r["status"]),
                total_fetched=int(r["total_fetched"] or 0),
                summarized=int(r["summarized"] or 0),
            )
            for r in rows
        ]
        hits = detect_zero_output(runs, now=at)
        if not hits:
            return "要約0件の継続: なし"
        _log.warning("zero_output_detected", pipelines=[h.pipeline for h in hits])
        frags = [f"{h.pipeline}(取得{h.fetched}件/{h.runs}回)" for h in hits]
        return f"⚠️{WINDOW_HOURS}h 取得ありで要約0件: " + ", ".join(frags)
    except Exception as e:  # noqa: BLE001 — heartbeat 本体を止めない
        _log.warning("zero_output_line_failed", error_type=type(e).__name__)
        return None


def _as_utc(value: object) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


__all__ = [
    "BASELINE_DAYS",
    "MIN_RUNS",
    "WINDOW_HOURS",
    "RunTotals",
    "ZeroOutput",
    "build_zero_output_line",
    "detect_zero_output",
]
