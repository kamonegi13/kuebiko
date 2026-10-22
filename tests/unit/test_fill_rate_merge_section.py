"""週次監査の「統合の判断記録」の節 (2026-10-22)。

統合の判断記録 (event_merge_decisions) が吸収の件数に追いついているか、戻した件数、
要約埋込 (統合の ML 特徴) の被覆を 1 行で出す。記録が欠けた統合は戻せない・調べられないので、
被覆の落ちは WARN にする。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.storage.repo_merge_decisions import MergeDecisionRow
from src.storage.run_history import RunHistoryRepository
from src.ui.services.fill_rate_merge_section import (
    MergeAuditStats,
    collect_merge_audit_stats,
    merge_decision_line,
)

_NOW = datetime(2026, 10, 22, 6, 0, 0, tzinfo=UTC)


class TestMergeDecisionLine:
    def test_full_coverage_is_not_a_warning(self) -> None:
        line, warn = merge_decision_line(
            MergeAuditStats(absorbed=40, recorded=40, undone=1, emb_have=800, emb_total=1000)
        )

        assert warn is False
        assert "40/40" in line and "戻した 1" in line and "80%" in line

    def test_low_coverage_warns(self) -> None:
        _, warn = merge_decision_line(
            MergeAuditStats(absorbed=40, recorded=10, undone=0, emb_have=1, emb_total=10)
        )

        assert warn is True

    def test_few_absorptions_are_not_judged(self) -> None:
        """ノイズ。吸収が少ない週は被覆で WARN にしない。"""
        _, warn = merge_decision_line(
            MergeAuditStats(absorbed=3, recorded=0, undone=0, emb_have=0, emb_total=0)
        )

        assert warn is False

    def test_empty_embedding_denominator_does_not_divide_by_zero(self) -> None:
        line, _ = merge_decision_line(
            MergeAuditStats(absorbed=0, recorded=0, undone=0, emb_have=0, emb_total=0)
        )

        assert "要約埋込" in line


class TestCollectStats:
    def test_counts_absorptions_with_and_without_records(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "audit.db")
        for iid in ("ev-a", "ev-b", "ev-c"):
            repo.create_event_item(
                item_id=iid,
                origin="live",
                first_reported_at=_NOW,
                last_reported_at=_NOW,
                importance="medium",
                when=_NOW,
            )
        for iid in ("ev-b", "ev-c"):
            repo.update_event_item(iid, {"merged_into": "ev-a", "updated_at": _NOW})
        repo.record_merge_decisions(
            [
                MergeDecisionRow(
                    target_id="ev-a",
                    absorbed_id="ev-b",
                    run_kind="hourly",
                    edges_json="[]",
                    edge_count=0,
                    rescued=False,
                    flags_json="{}",
                )
            ],
            decided_at=_NOW,
        )

        stats = collect_merge_audit_stats(repo, since=datetime(2026, 10, 15, tzinfo=UTC))

        assert stats.absorbed == 2
        assert stats.recorded == 1
        assert stats.undone == 0
