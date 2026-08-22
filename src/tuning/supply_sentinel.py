"""E1 ラベル供給のドリフト番兵 (§13-1 対処の残、2026-08-22)。

E1 突合キー (victim_org) は summarizer の LLM 出力に依存する閉ループ (§13-1)。
rubric 変更でラベル供給が静かに縮退しても、収穫は「新規 0 件」を正常として
報告し続けてしまう — 供給量そのものを週次で監視し、崩落を警告する。

判定は fill-rate 監査と同じ思想 (過去週中央値との比較・決定論・LLM 不使用)。
ラベルの arrived_at (= 記事の実効日) で週バケツを作り、直近の完全週が
過去 4 週中央値の半分未満なら警告行を返す。母数が小さすぎる週は判定しない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

# 崩落判定: 直近完全週 < 中央値 × この比率
_COLLAPSE_RATIO = 0.5
# 過去週の中央値がこの件数未満なら判定しない (元々供給が薄い週はノイズ)
_MIN_BASELINE_PER_WEEK = 3
_BASELINE_WEEKS = 4
_FETCH_LIMIT = 10_000


def _week_start(dt: datetime) -> datetime:
    """その時刻が属する週 (月曜 00:00 UTC) の先頭。"""
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def supply_drift_lines(repo: Any, *, now: datetime | None = None) -> list[str]:
    """週次収穫の ops 通知に足す供給状況の行 (常に 1 行 + 崩落時は警告行)。

    失敗は空 list (fail-open — 番兵の障害で収穫通知を止めない)。
    """
    try:
        base = now or datetime.now(UTC)
        rows = repo.list_tuning_labels(field="subject_actor", limit=_FETCH_LIMIT)
        counts: dict[datetime, int] = {}
        for r in rows:
            if r.get("source") != "E1" or r.get("superseded_by") is not None:
                continue
            try:
                dt = datetime.fromisoformat(str(r["arrived_at"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            counts[_week_start(dt)] = counts.get(_week_start(dt), 0) + 1

        this_week = _week_start(base)
        latest_full = this_week - timedelta(weeks=1)
        latest_n = counts.get(latest_full, 0)
        baseline = [
            counts.get(latest_full - timedelta(weeks=i), 0) for i in range(1, _BASELINE_WEEKS + 1)
        ]
        base_median = median(baseline) if baseline else 0.0

        lines = [
            f"E1 供給: 直近完全週 {latest_n} 件 (過去 {_BASELINE_WEEKS} 週中央値 "
            f"{base_median:.0f} 件)"
        ]
        if base_median >= _MIN_BASELINE_PER_WEEK and latest_n < base_median * _COLLAPSE_RATIO:
            lines.append(
                f"⚠️ E1 供給の崩落疑い: 直近完全週 {latest_n} 件 < 中央値の半分。"
                " rubric 変更による victim_org 抽出の縮退 (§13-1 閉ループ) を確認してください"
            )
        return lines
    except Exception as e:  # noqa: BLE001 — 番兵の失敗で収穫通知を止めない
        _log.warning("supply_sentinel_failed", error=str(e)[:80])
        return []
