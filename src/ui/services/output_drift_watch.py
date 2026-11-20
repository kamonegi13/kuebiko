"""切替の前後で出力の分布を比べる見張り (2026-11-20、11-19 監査 D4 P1-5 (b) の最小構成)。

11-17 の s26r 切替の後、要約の importance の low 率が 24% → 39% にずれたが、切替を起点に前後を
比べる仕組みが無く 2 日気付かなかった。ここでは設計 (D4 §P1-5 (b)) のうち **事象起点の前後比較**
だけを、新しい表もジョブも足さずに作る (常時の MAD 検出・日次の指標表は段 2)。

- 事象: モデル割当 (``model_tiers``)・判定基準 (``*_rubric``) の版、構成変更のうち旗が変わったもの
  (``runtime_change_events``。コードだけのデプロイは毎日あるので数えない)。
  6 時間以内の事象は 1 つに束ねる。
- 比較: 事象の前 ``WINDOW_DAYS`` 日と後 ``WINDOW_DAYS`` 日 (配信した記事。Grok・分析専用を除く)。
  low 率は **feed の構成をそろえた値** (前の窓の feed の件数で直接標準化) も出す
  — feed の構成が変わっただけで鳴らないように。
- 出す時: 後の窓が閉じた翌日のハートビートに 1 回 (変化が無くても「変化なし」と出す)。
  窓の中に別の事象があれば「混成」と注記する。
- ⚠ の線 (**暫定**、結果を見る前に決めた値。93 日の再現で決め直すのは段 2): feed をそろえた
  low 率か high 率が ``RATE_WARN`` 以上動く、または要約長の中央値が ``LENGTH_WARN`` 以上動く。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

WINDOW_DAYS = 3
CLUSTER_HOURS = 6
RATE_WARN = 0.10
LENGTH_WARN = 0.30
MIN_ARTICLES = 50

_ARTICLES_SQL = (
    "SELECT feed_title, importance, LENGTH(COALESCE(summary, '')) AS n_chars FROM articles"
    " WHERE status = 'posted' AND analysis_only IS NULL AND article_id NOT LIKE ?"
    " AND created_at >= ? AND created_at < ?"
)
_GROK_LIKE = "grok%"


@dataclass(frozen=True)
class ChangeEvent:
    at: datetime
    label: str


@dataclass(frozen=True)
class WindowStats:
    n: int
    low: float
    high: float
    low_std: float  # 前の窓の feed の構成にそろえた low 率 (前の窓では素の値と同じ)
    median_chars: float


@dataclass(frozen=True)
class Row:
    feed: str
    importance: str
    chars: int


def _as_utc(value: object) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def cluster_events(events: Sequence[ChangeEvent]) -> list[ChangeEvent]:
    """``CLUSTER_HOURS`` 以内に続く事象を最初の時刻に束ねる (純粋関数)。"""
    out: list[ChangeEvent] = []
    for ev in sorted(events, key=lambda e: e.at):
        if out and ev.at - out[-1].at <= timedelta(hours=CLUSTER_HOURS):
            last = out[-1]
            if ev.label not in last.label:
                out[-1] = ChangeEvent(last.at, f"{last.label}・{ev.label}")
            continue
        out.append(ev)
    return out


def window_stats(rows: Sequence[Row], *, feed_weights: Counter[str] | None = None) -> WindowStats:
    """窓の指標。``feed_weights`` (前の窓の feed ごとの件数) で low 率を直接標準化する。"""
    n = len(rows)
    low = sum(r.importance == "low" for r in rows) / n
    high = sum(r.importance == "high" for r in rows) / n
    chars = float(median(r.chars for r in rows))
    if not feed_weights:
        return WindowStats(n, low, high, low, chars)
    by_feed: dict[str, list[Row]] = {}
    for r in rows:
        by_feed.setdefault(r.feed, []).append(r)
    num = den = 0.0
    for feed, w in feed_weights.items():
        group = by_feed.get(feed)
        if not group:
            continue
        num += w * sum(r.importance == "low" for r in group) / len(group)
        den += w
    return WindowStats(n, low, high, num / den if den else low, chars)


def _fetch(repo: RunHistoryRepository, start: datetime, end: datetime) -> list[Row]:
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        rows = conn.execute(
            _ARTICLES_SQL, (_GROK_LIKE, start.isoformat(), end.isoformat())
        ).fetchall()
    return [
        Row(str(r["feed_title"] or ""), str(r["importance"] or ""), int(r["n_chars"] or 0))
        for r in rows
    ]


def _collect_events(repo: RunHistoryRepository, since: datetime) -> list[ChangeEvent]:
    events: list[ChangeEvent] = []
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        for r in conn.execute(
            "SELECT config_key, version, created_at FROM app_config_versions"
            " WHERE created_at >= ? AND (config_key = 'model_tiers' OR config_key LIKE ?)",
            (since.isoformat(), "%rubric%"),
        ).fetchall():
            label = f"{r['config_key']} 版{r['version']}"
            events.append(ChangeEvent(_as_utc(r["created_at"]), label))
        for r in conn.execute(
            "SELECT recorded_at, detail FROM runtime_change_events WHERE recorded_at >= ?",
            (since.isoformat(),),
        ).fetchall():
            if '"changed_flags": {}' not in str(r["detail"]):
                events.append(ChangeEvent(_as_utc(r["recorded_at"]), "旗の変更"))
    return events


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def compare_event(
    repo: RunHistoryRepository, event: ChangeEvent, others: Sequence[ChangeEvent]
) -> str | None:
    """1 事象の前後比較の文。どちらかの窓が ``MIN_ARTICLES`` 件未満なら None。"""
    span = timedelta(days=WINDOW_DAYS)
    before = _fetch(repo, event.at - span, event.at)
    after = _fetch(repo, event.at, event.at + span)
    if len(before) < MIN_ARTICLES or len(after) < MIN_ARTICLES:
        return None
    pre = window_stats(before)
    post = window_stats(after, feed_weights=Counter(r.feed for r in before))
    moved = (
        abs(post.low_std - pre.low) >= RATE_WARN
        or abs(post.high - pre.high) >= RATE_WARN
        or abs(post.median_chars - pre.median_chars) >= LENGTH_WARN * max(pre.median_chars, 1)
    )
    mixed = any(o.at != event.at and abs(o.at - event.at) < span for o in others)
    when = event.at.astimezone(UTC).strftime("%m-%d")
    body = (
        f"low {_pct(pre.low)}→{_pct(post.low)} (feed をそろえて {_pct(post.low_std)})・"
        f"high {_pct(pre.high)}→{_pct(post.high)}・"
        f"要約の中央値 {round(pre.median_chars)}→{round(post.median_chars)} 字"
    )
    head = "⚠️" if moved else ""
    verdict = "" if moved else "変化なし: "
    note = " (混成: 窓の中に別の変更あり)" if mixed else ""
    return f"{head}切替の前後 {when} {event.label} ±{WINDOW_DAYS}日: {verdict}{body}{note}"


def build_output_drift_line(
    repo: RunHistoryRepository, *, now: datetime | None = None
) -> str | None:
    """ハートビートの 1 行。後の窓が直近 24 時間に閉じた事象が無ければ None。"""
    try:
        at = now or datetime.now(UTC)
        span = timedelta(days=WINDOW_DAYS)
        events = cluster_events(_collect_events(repo, at - 2 * span - timedelta(days=1)))
        due = [e for e in events if at - span - timedelta(days=1) <= e.at < at - span]
        lines = [line for e in due if (line := compare_event(repo, e, events))]
        if lines:
            _log.info("output_drift_compared", events=len(due), warned=sum("⚠" in x for x in lines))
        return "\n".join(lines) or None
    except Exception as e:  # noqa: BLE001 — heartbeat 本体を止めない
        _log.warning("output_drift_line_failed", error_type=type(e).__name__)
        return None


__all__ = [
    "ChangeEvent",
    "LENGTH_WARN",
    "RATE_WARN",
    "Row",
    "WINDOW_DAYS",
    "build_output_drift_line",
    "cluster_events",
    "compare_event",
    "window_stats",
]
