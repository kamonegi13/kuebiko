"""統合を戻す操作 (2026-10-22)。1 か所の関数 ``undo_merge`` に寄せる。

統合は吸収側の行を残す (``merged_into`` を立てるだけ) ので、戻すとは
(1) 吸収側の ``merged_into`` を外す (2) 統合先 (と、その先に吸収されている祖先) から
``retro_merge`` で足されたメンバー行を消す (3) 祖先の状態を作り直す、の 3 つ。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.eventnews.models import MemberArticle
from src.storage.repo_merge_decisions import MergeDecisionRow
from src.storage.run_history import RunHistoryRepository
from src.ui.services.eventnews_merge_job import JOIN_SIGNAL
from src.ui.services.eventnews_merge_undo import undo_merge

_NOW = datetime(2026, 10, 22, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "undo.db")


def _fake_loader(_repo: object, ids: list[str], _counts: object) -> dict[str, MemberArticle]:
    return {
        a: MemberArticle(
            article_id=a,
            title=a,
            url=f"https://x.example/{a}",
            feed_title="F",
            feed_url="https://x.example/f",
            host="x.example",
            importance="high" if a.startswith("y") else "medium",
            category="apt",
            status="posted",
            anchor_ts=_NOW,
            summary="s",
            body="b",
            entities=frozenset(),
        )
        for a in ids
    }


def _item(repo: RunHistoryRepository, iid: str) -> None:
    repo.create_event_item(
        item_id=iid,
        origin="live",
        first_reported_at=_NOW,
        last_reported_at=_NOW,
        importance="low",
        when=_NOW,
    )
    repo.update_event_item(iid, {"current_version": 3})


def _member(repo: RunHistoryRepository, iid: str, aid: str, signal: str) -> None:
    repo.add_event_member(
        item_id=iid, article_id=aid, joined_at=_NOW, contributed_new_facts=0, join_signal=signal
    )


def _merge(repo: RunHistoryRepository) -> None:
    """a ← b (b は y1/y2 を持つ)。a は元から x1。"""
    for iid in ("ev-a", "ev-b"):
        _item(repo, iid)
    _member(repo, "ev-a", "x1", "seed")
    _member(repo, "ev-b", "y1", "seed")
    _member(repo, "ev-b", "y2", "seed")
    _member(repo, "ev-a", "y1", JOIN_SIGNAL)
    _member(repo, "ev-a", "y2", JOIN_SIGNAL)
    repo.update_event_item("ev-b", {"merged_into": "ev-a"})
    repo.record_merge_decisions(
        [
            MergeDecisionRow(
                target_id="ev-a",
                absorbed_id="ev-b",
                run_kind="hourly",
                edges_json="[]",
                edge_count=2,
                rescued=False,
                flags_json="{}",
            )
        ],
        decided_at=_NOW,
    )


def _members(repo: RunHistoryRepository, iid: str) -> set[str]:
    return {m.article_id for m in repo.list_event_members(iid)}


class TestUndoMerge:
    def test_restores_the_absorbed_item_and_strips_the_target(
        self, repo: RunHistoryRepository
    ) -> None:
        _merge(repo)

        res = undo_merge(repo, "ev-b", reason="hub", now=_NOW, load_members=_fake_loader)

        assert res.status == "restored"
        b = repo.get_event_item("ev-b")
        assert b is not None and b.merged_into is None
        assert _members(repo, "ev-a") == {"x1"}
        assert _members(repo, "ev-b") == {"y1", "y2"}  # 吸収側の行は触らない
        a = repo.get_event_item("ev-a")
        assert a is not None
        assert a.state.current_version == 0  # 本文を作り直させる
        assert a.merged_into is None
        assert repo.list_merge_decisions(active_only=True) == []
        assert repo.list_merge_decisions(absorbed_id="ev-b")[0].undone_reason == "hub"

    def test_dry_run_writes_nothing(self, repo: RunHistoryRepository) -> None:
        _merge(repo)

        res = undo_merge(repo, "ev-b", reason="x", now=_NOW, apply=False, load_members=_fake_loader)

        assert res.status == "dry_run"
        assert res.ancestors == ("ev-a",)
        assert res.removed_rows == 2
        b = repo.get_event_item("ev-b")
        assert b is not None and b.merged_into == "ev-a"
        assert _members(repo, "ev-a") == {"x1", "y1", "y2"}
        assert len(repo.list_merge_decisions(active_only=True)) == 1

    def test_not_merged_item_is_left_alone(self, repo: RunHistoryRepository) -> None:
        _item(repo, "ev-a")

        res = undo_merge(repo, "ev-a", reason="x", now=_NOW, load_members=_fake_loader)

        assert res.status == "not_merged"

    def test_unknown_item_reports_missing(self, repo: RunHistoryRepository) -> None:
        res = undo_merge(repo, "nope", reason="x", now=_NOW, load_members=_fake_loader)

        assert res.status == "missing"

    def test_chain_strips_every_ancestor_but_keeps_deeper_merges(
        self, repo: RunHistoryRepository
    ) -> None:
        """c → b → a。b を戻すと a から b と c の記事が消え、c は b に吸収されたまま。"""
        _merge(repo)
        _item(repo, "ev-c")
        _member(repo, "ev-c", "z1", "seed")
        _member(repo, "ev-b", "z1", JOIN_SIGNAL)
        _member(repo, "ev-a", "z1", JOIN_SIGNAL)
        repo.update_event_item("ev-c", {"merged_into": "ev-b"})

        undo_merge(repo, "ev-b", reason="x", now=_NOW, load_members=_fake_loader)

        assert _members(repo, "ev-a") == {"x1"}
        assert _members(repo, "ev-b") == {"y1", "y2", "z1"}
        c = repo.get_event_item("ev-c")
        assert c is not None and c.merged_into == "ev-b"

    def test_original_members_of_the_target_survive_even_if_ids_overlap(
        self, repo: RunHistoryRepository
    ) -> None:
        _merge(repo)
        # y1 は統合先の元からのメンバーでもある (retro_merge の行ではない)
        repo.delete_event_members("ev-a", ["y1"], only_join_signal=JOIN_SIGNAL)
        _member(repo, "ev-a", "y1", "seed")

        undo_merge(repo, "ev-b", reason="x", now=_NOW, load_members=_fake_loader)

        assert _members(repo, "ev-a") == {"x1", "y1"}

    def test_target_importance_is_recomputed_from_the_remaining_members(
        self, repo: RunHistoryRepository
    ) -> None:
        _merge(repo)
        repo.update_event_item("ev-a", {"importance": "high"})  # 統合で y (high) が効いた状態

        undo_merge(repo, "ev-b", reason="x", now=_NOW, load_members=_fake_loader)

        a = repo.get_event_item("ev-a")
        assert a is not None and a.state.importance == "medium"
