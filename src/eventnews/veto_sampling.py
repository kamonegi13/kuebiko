"""統合で止めた組の抜き取り (Opus 裁定の入力) を作る (2026-10-24、11-20 にジョブ化で共通化)。

docs/eventnews_focus_redesign.md §9.4 の「週 10 組を抜いて裁定」。``scripts/sample_focus_vetoes.py``
(手で回す道具) と週次ジョブ ``weekly-veto-sample`` (``ui/services/veto_sample_job``) の両方が
ここを使う。LLM は呼ばない。抜いた組に ``sampled_at`` を付けるかは呼び手が決める。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from src.eventnews.identity.focus import focus_to_dict
from src.eventnews.identity.replay import pick_sample
from src.eventnews.models import ENTITY_FREQ_WINDOW_HOURS
from src.storage.run_history import RunHistoryRepository

DEFAULT_COUNT = 10
DEFAULT_LOOKBACK_DAYS = 14
_TITLES_PER_ITEM = 5
#: 抜く対象の action。shadow 中の記録・焦点の conflict で止めた組・勧告のハブで止めた組
#: (11-20 に hub_split_blocked を足した — 止めている組の 3 割がこれで、裁定の外にあった)
SAMPLED_ACTIONS = ("blocked", "shadow", "hub_split_blocked")


def week_seed(now: datetime) -> int:
    iso = now.isocalendar()
    return iso.year * 100 + iso.week


def week_label(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _describe_item(
    repo: RunHistoryRepository, item_id: str, counts: dict[tuple[str, str], int]
) -> dict[str, Any]:
    # 重い import (群化のジョブ一式) は呼ばれたときだけ
    from src.ui.services.eventnews_focus_job import focus_of_members, order_members
    from src.ui.services.eventnews_hourly_job import _load_members

    rows = repo.list_members_in_join_order([item_id]).get(item_id, [])
    by_id = _load_members(repo, [a for a, _ in rows], counts)
    members = order_members(rows, by_id)
    return {
        "item_id": item_id,
        "article_count": len(members),
        "titles": [m.title for m in members[:_TITLES_PER_ITEM]],
        "focus": focus_to_dict(focus_of_members(members)),
    }


def build_sample(
    repo: RunHistoryRepository,
    *,
    now: datetime,
    count: int = DEFAULT_COUNT,
    days: int = DEFAULT_LOOKBACK_DAYS,
    seed: int = 0,
) -> tuple[list[tuple[str, str]], list[str], int]:
    """(抜いた組, jsonl の行, 未抽出の組の数) を返す。DB は読むだけ。"""
    from src.ui.services.eventnews_hourly_job import _entity_counts

    rows = repo.list_merge_vetoes(
        since=now - timedelta(days=days), actions=SAMPLED_ACTIONS, unsampled_only=True
    )
    by_key = {(r.item_a, r.item_b): r for r in rows}
    picked = pick_sample(list(by_key), count, seed=seed or week_seed(now))
    if not picked:
        return [], [], len(by_key)
    counts = _entity_counts(repo, now - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    lines = []
    for key in picked:
        row = by_key[key]
        record = {
            "item_a": row.item_a,
            "item_b": row.item_b,
            "action": row.action,
            "klass": row.klass,
            "detail": row.detail,
            "edge_count": row.edge_count,
            "times_seen": row.times_seen,
            "a": _describe_item(repo, row.item_a, counts),
            "b": _describe_item(repo, row.item_b, counts),
        }
        lines.append(json.dumps(record, ensure_ascii=False))
    return picked, lines, len(by_key)


__all__ = [
    "DEFAULT_COUNT",
    "DEFAULT_LOOKBACK_DAYS",
    "SAMPLED_ACTIONS",
    "build_sample",
    "week_label",
    "week_seed",
]
