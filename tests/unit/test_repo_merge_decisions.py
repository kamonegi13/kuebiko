"""統合の判断記録 (event_merge_decisions) と、統合を戻すための repo 操作 (2026-10-22)。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.repo_merge_decisions import MergeDecisionRow
from src.storage.run_history import RunHistoryRepository

_NOW = datetime(2026, 10, 22, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "merge_decisions.db")


def _row(absorbed: str = "ev-b", target: str = "ev-a") -> MergeDecisionRow:
    return MergeDecisionRow(
        target_id=target,
        absorbed_id=absorbed,
        run_kind="hourly",
        edges_json='[{"a":"x1","b":"y1","cos":0.8,"proba":0.9,"source":"ml"}]',
        edge_count=1,
        rescued=True,
        flags_json='{"cve_gate":true}',
    )


class TestMergeDecisions:
    def test_recorded_decision_is_listed_as_active(self, repo: RunHistoryRepository) -> None:
        repo.record_merge_decisions([_row()], decided_at=_NOW)

        got = repo.list_merge_decisions(active_only=True)

        assert len(got) == 1
        assert (got[0].target_id, got[0].absorbed_id) == ("ev-a", "ev-b")
        assert got[0].rescued is True
        assert got[0].edge_count == 1
        assert got[0].undone_at is None
        assert got[0].decided_at == _NOW

    def test_filter_by_target_and_absorbed(self, repo: RunHistoryRepository) -> None:
        repo.record_merge_decisions(
            [_row("ev-b", "ev-a"), _row("ev-c", "ev-a"), _row("ev-e", "ev-d")], decided_at=_NOW
        )

        assert {d.absorbed_id for d in repo.list_merge_decisions(target_id="ev-a")} == {
            "ev-b",
            "ev-c",
        }
        assert [d.target_id for d in repo.list_merge_decisions(absorbed_id="ev-e")] == ["ev-d"]

    def test_mark_undone_hides_it_from_active_but_keeps_the_row(
        self, repo: RunHistoryRepository
    ) -> None:
        repo.record_merge_decisions([_row()], decided_at=_NOW)

        n = repo.mark_merge_decisions_undone("ev-b", reason="wrong hub", undone_at=_NOW)

        assert n == 1
        assert repo.list_merge_decisions(active_only=True) == []
        (kept,) = repo.list_merge_decisions(active_only=False)
        assert kept.undone_reason == "wrong hub"
        assert kept.undone_at == _NOW

    def test_empty_batch_is_a_noop(self, repo: RunHistoryRepository) -> None:
        assert repo.record_merge_decisions([], decided_at=_NOW) == 0

    def test_counts_per_run_kind_since(self, repo: RunHistoryRepository) -> None:
        repo.record_merge_decisions([_row("ev-b"), _row("ev-c")], decided_at=_NOW)

        got = repo.count_merge_decisions_since(datetime(2026, 10, 1, tzinfo=UTC))

        assert got == {"active": 2, "undone": 0}


class TestDeleteMergedMembers:
    def _seed(self, repo: RunHistoryRepository) -> None:
        for iid in ("ev-a", "ev-b"):
            repo.create_event_item(
                item_id=iid,
                origin="live",
                first_reported_at=_NOW,
                last_reported_at=_NOW,
                importance="medium",
                when=_NOW,
            )
        repo.add_event_member(
            item_id="ev-a",
            article_id="x1",
            joined_at=_NOW,
            contributed_new_facts=0,
            join_signal="seed",
        )
        for aid in ("y1", "y2"):
            repo.add_event_member(
                item_id="ev-a",
                article_id=aid,
                joined_at=_NOW,
                contributed_new_facts=0,
                join_signal="retro_merge",
            )

    def test_deletes_only_rows_with_the_given_signal(self, repo: RunHistoryRepository) -> None:
        self._seed(repo)

        n = repo.delete_event_members("ev-a", ["x1", "y1"], only_join_signal="retro_merge")

        assert n == 1  # x1 は seed なので残る
        left = {m.article_id for m in repo.list_event_members("ev-a")}
        assert left == {"x1", "y2"}


class TestEventRoots:
    def test_follows_merged_into_to_the_live_root(self, repo: RunHistoryRepository) -> None:
        for iid in ("ev-a", "ev-b", "ev-c"):
            repo.create_event_item(
                item_id=iid,
                origin="live",
                first_reported_at=_NOW,
                last_reported_at=_NOW,
                importance="medium",
                when=_NOW,
            )
        repo.add_event_member(
            item_id="ev-c", article_id="z1", joined_at=_NOW, contributed_new_facts=0, join_signal=""
        )
        repo.add_event_member(
            item_id="ev-a", article_id="x1", joined_at=_NOW, contributed_new_facts=0, join_signal=""
        )
        repo.update_event_item("ev-c", {"merged_into": "ev-b"})
        repo.update_event_item("ev-b", {"merged_into": "ev-a"})

        got = repo.event_roots_by_article(["z1", "x1", "none"])

        assert got == {"z1": "ev-a", "x1": "ev-a"}  # 事象に入っていない記事は欠ける
