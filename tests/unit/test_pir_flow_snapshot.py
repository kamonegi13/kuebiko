"""情報フロー集計 (compute_flow_snapshot の純粋部) のテスト。

PIR → importance → channel の背骨をエッジ実数で描くための集計。
rows は dict (sqlite3.Row 互換の [key] アクセス) で供給する。
"""

from __future__ import annotations

import sqlite3
from typing import cast

from src.pir.evaluator import FlowSnapshot, PirFlowStat, flow_snapshot_from_rows
from src.pir.models import Pir, StrongSignals


def _row(
    article_id: str,
    *,
    importance: str = "medium",
    channel: str | None = "brief",
    title: str = "",
    feed_title: str = "",
) -> sqlite3.Row:
    # dict は sqlite3.Row 互換の [key] アクセスを持つテスト用スタブ (docstring 参照)
    return cast(
        sqlite3.Row,
        {
            "article_id": article_id,
            "title": title,
            "summary": "",
            "body": "",
            "feed_title": feed_title,
            "importance": importance,
            "posted_channel": channel,
            "victim_sector_canonical": "",
            "victim_country_iso": "",
        },
    )


def _pir(pid: str, keywords: list[str]) -> Pir:
    return Pir(id=pid, title=pid, strong_signals=StrongSignals(keywords=keywords))


class TestFlowSnapshotFromRows:
    def test_empty_rows(self) -> None:
        snap = flow_snapshot_from_rows([_pir("p1", ["ransomware"])], [], {})
        assert snap == FlowSnapshot(
            total_articles=0,
            importance_totals={},
            importance_channel=[],
            pir_stats=[PirFlowStat("p1", 0, {}, {})],
        )

    def test_importance_channel_matrix(self) -> None:
        rows = [
            _row("a1", importance="high", channel="alert"),
            _row("a2", importance="high", channel="brief"),
            _row("a3", importance="medium", channel="brief"),
            _row("a4", importance="low", channel="watch"),
            _row("a5", importance="low", channel="watch"),
        ]
        snap = flow_snapshot_from_rows([], rows, {})
        assert snap.total_articles == 5
        assert snap.importance_totals == {"high": 2, "medium": 1, "low": 2}
        assert ("high", "alert", 1) in snap.importance_channel
        assert ("low", "watch", 2) in snap.importance_channel

    def test_pir_match_breakdown(self) -> None:
        rows = [
            _row("a1", importance="high", channel="alert", title="LockBit ransomware attack"),
            _row("a2", importance="medium", channel="brief", title="ransomware report"),
            _row("a3", importance="low", channel="watch", title="cloud cost tutorial"),
        ]
        snap = flow_snapshot_from_rows([_pir("p_ransom", ["ransomware"])], rows, {})
        stat = snap.pir_stats[0]
        assert stat.pir_id == "p_ransom"
        assert stat.matched_total == 2
        assert stat.by_importance == {"high": 1, "medium": 1}
        assert stat.by_channel == {"alert": 1, "brief": 1}

    def test_pir_without_strong_signals_is_zero(self) -> None:
        rows = [_row("a1", title="anything")]
        pir = Pir(id="p_empty", title="empty")
        snap = flow_snapshot_from_rows([pir], rows, {})
        assert snap.pir_stats[0].matched_total == 0

    def test_unknown_importance_and_channel_bucketed(self) -> None:
        rows = [_row("a1", importance="", channel=None)]
        snap = flow_snapshot_from_rows([], rows, {})
        assert snap.importance_totals == {"unknown": 1}
        assert snap.importance_channel == [("unknown", "unknown", 1)]

    def test_actor_map_used_for_actor_matching(self) -> None:
        rows = [_row("a1", importance="high", channel="alert", title="campaign report")]
        pir = Pir(id="p_actor", title="actor", strong_signals=StrongSignals(actors=["APT41"]))
        snap = flow_snapshot_from_rows([pir], rows, {"a1": {"apt41"}})
        assert snap.pir_stats[0].matched_total == 1
