"""週次 fill-rate 監査の節: 事象の焦点の被覆 (2026-10-24 F0)。

必須 3 点セット (CLAUDE.md §7) の「fill-rate 週次監査へ登録」。複数記事の生きた事象のうち
焦点が空でない割合 (F0 の合格線 95%) と、外れが 20% 以上の事象の数 (分割の候補の目安) を
1 行で出す。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 焦点が空でない割合がこれ未満なら WARN (docs/eventnews_focus_redesign.md §7 F0 の進む条件)
MIN_NON_EMPTY_RATIO = 0.95
#: 判定する最小の事象数 (これ未満はノイズ)
MIN_ITEMS_TO_JUDGE = 20
#: 「外れが多い」とみなす外れの割合 (§4.1)
OUTSIDER_RATIO = 0.2
_PCT = 100


@dataclass(frozen=True)
class FocusAuditStats:
    multi_items: int
    recorded: int
    non_empty: int
    heavy_outsiders: int


def collect_focus_audit_stats(repo: Any) -> FocusAuditStats:
    total, non_empty, heavy = repo.count_focus_audit(outsider_ratio=OUTSIDER_RATIO)
    with repo._connect() as conn:  # noqa: SLF001 — 監査の読み取り
        recorded = conn.execute(
            "SELECT COUNT(*) AS n FROM event_item_focus f JOIN event_items i ON i.id = f.item_id"
            " WHERE i.merged_into IS NULL"
        ).fetchone()["n"]
    return FocusAuditStats(
        multi_items=int(total),
        recorded=int(recorded),
        non_empty=int(non_empty),
        heavy_outsiders=int(heavy),
    )


def focus_line(stats: FocusAuditStats) -> tuple[str, bool]:
    """(表示行, WARN か)。分母は記録済みの事象 (未記録は別に件数を出す)。"""
    pct = round(_PCT * stats.non_empty / stats.recorded) if stats.recorded else 0
    line = (
        f"事象の焦点: 複数記事の事象 {stats.multi_items} 件のうち記録済み {stats.recorded}、"
        f"焦点が空でないもの {stats.non_empty} ({pct}%)・外れ "
        f"{round(_PCT * OUTSIDER_RATIO)}% 以上 {stats.heavy_outsiders} 件"
    )
    judged = stats.recorded >= MIN_ITEMS_TO_JUDGE
    warn = judged and stats.non_empty < MIN_NON_EMPTY_RATIO * stats.recorded
    if warn:
        line = "⚠ " + line + " — 名前を取れていない事象が多い (entity 抽出か規則を確認)"
    return line, warn
