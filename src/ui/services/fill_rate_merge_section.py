"""週次 fill-rate 監査の節: 統合の判断記録の被覆 (2026-10-22)。

新しい分析列の必須 3 点セット (CLAUDE.md §7) の「fill-rate 週次監査へ登録」。
``event_merge_decisions`` は吸収 1 件ごとに 1 行を書く。吸収の件数に記録が追いついていないと、
その統合は根拠を辿れず、戻す対象を選べない。あわせて統合の ML 特徴 (要約埋込) の被覆を出す
(``EVENTNEWS_MERGE_REAL_KIND`` で毎時埋める。被覆が伸びなければ埋込の基盤が止まっている)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

#: 吸収がこれ未満の週は被覆で判定しない (ノイズ)
MIN_ABSORBED_TO_JUDGE = 10
#: 記録の被覆がこれ未満なら WARN
MIN_RECORD_COVERAGE = 0.9
#: 要約埋込の被覆を数える事象の最終報道の窓 (日)
EMBED_WINDOW_DAYS = 30
_PCT = 100


@dataclass(frozen=True)
class MergeAuditStats:
    absorbed: int
    recorded: int
    undone: int
    emb_have: int
    emb_total: int


def collect_merge_audit_stats(repo: Any, *, since: datetime) -> MergeAuditStats:
    """``since`` 以降の吸収・記録・戻しの件数と、直近 30 日の事象メンバーの要約埋込の被覆。"""
    since_iso = since.isoformat()
    emb_since = (since + timedelta(days=7) - timedelta(days=EMBED_WINDOW_DAYS)).isoformat()
    with repo._connect() as conn:  # noqa: SLF001 — 監査の読み取り
        absorbed = conn.execute(
            "SELECT COUNT(*) AS n FROM event_items WHERE merged_into IS NOT NULL"
            " AND updated_at >= ?",
            (since_iso,),
        ).fetchone()["n"]
        recorded = conn.execute(
            "SELECT COUNT(*) AS n FROM event_items i WHERE i.merged_into IS NOT NULL"
            " AND i.updated_at >= ? AND EXISTS (SELECT 1 FROM event_merge_decisions d"
            " WHERE d.absorbed_id = i.id AND d.decided_at >= ?)",
            (since_iso, since_iso),
        ).fetchone()["n"]
        undone = conn.execute(
            "SELECT COUNT(*) AS n FROM event_merge_decisions WHERE undone_at IS NOT NULL"
            " AND undone_at >= ?",
            (since_iso,),
        ).fetchone()["n"]
        emb = conn.execute(
            "SELECT COUNT(DISTINCT m.article_id) AS total, COUNT(DISTINCT s.article_id) AS have"
            " FROM event_item_members m JOIN event_items i ON i.id = m.item_id"
            " LEFT JOIN summary_embeddings s ON s.article_id = m.article_id"
            " WHERE i.merged_into IS NULL AND i.last_reported_at >= ?",
            (emb_since,),
        ).fetchone()
    return MergeAuditStats(
        absorbed=int(absorbed),
        recorded=int(recorded),
        undone=int(undone),
        emb_have=int(emb["have"]),
        emb_total=int(emb["total"]),
    )


def merge_decision_line(stats: MergeAuditStats) -> tuple[str, bool]:
    """(表示行, WARN か)。吸収が少ない週は WARN にしない。"""
    pct = round(_PCT * stats.emb_have / stats.emb_total) if stats.emb_total else 0
    line = (
        f"統合の判断記録 (7日): 吸収 {stats.absorbed} 件のうち記録あり "
        f"{stats.recorded}/{stats.absorbed}・戻した {stats.undone}。"
        f"統合の要約埋込の被覆 {pct}% ({stats.emb_have}/{stats.emb_total})"
    )
    judged = stats.absorbed >= MIN_ABSORBED_TO_JUDGE
    warn = judged and stats.recorded < MIN_RECORD_COVERAGE * stats.absorbed
    if warn:
        line = "⚠ " + line + " — 記録が欠けた統合は根拠を辿れず戻せない"
    return line, warn
