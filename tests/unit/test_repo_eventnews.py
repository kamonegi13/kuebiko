"""event news storage 層 (src/storage/repo_eventnews.py) の unit test。

設計 SSoT: docs/event_news_design.md §6/§7/§9/§11。v1 は shadow (本番非接続) だが
スキーマの読み書き・冪等性・版管理の不変条件はここで固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.pipeline.filters import SemanticSkip
from src.storage.run_history import RunHistoryRepository

_NOW = datetime(2026, 8, 23, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "eventnews.db")


def _skip(*, tier: str = "hard", matched_key: str = "abc123") -> SemanticSkip:
    return SemanticSkip(
        skipped_url="https://x.example/1",
        skipped_title="t",
        skipped_host="x.example",
        feed_title="Feed",
        feed_url="https://x.example/feed",
        tier=tier,  # type: ignore[arg-type]
        matched_kind="url_hash",
        matched_key=matched_key,
    )


class TestEventItemCRUD:
    def test_create_and_get_event_item(self, repo: RunHistoryRepository) -> None:
        repo.create_event_item(
            item_id="evt1",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="high",
            when=_NOW,
        )
        rec = repo.get_event_item("evt1")
        assert rec is not None
        assert rec.state.item_id == "evt1"
        assert rec.state.status == "new"
        assert rec.state.current_version == 0  # §7: 未生成 = 無条件再生成対象
        assert rec.state.importance == "high"
        assert rec.origin == "live"
        assert rec.state.member_ids == ()
        assert rec.independent_sources == 0
        assert rec.state_media_count == 0
        assert rec.unclassified_sources == 0
        assert rec.change_kind is None
        assert rec.merged_into is None

    def test_get_missing_item_returns_none(self, repo: RunHistoryRepository) -> None:
        assert repo.get_event_item("nope") is None

    def test_list_event_items_filters_by_origin(self, repo: RunHistoryRepository) -> None:
        repo.create_event_item(
            item_id="live1",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="high",
        )
        repo.create_event_item(
            item_id="replay1",
            origin="replay",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="high",
        )

        live_items = repo.list_event_items(origin="live")
        assert {i.state.item_id for i in live_items} == {"live1"}

        replay_items = repo.list_event_items(origin="replay")
        assert {i.state.item_id for i in replay_items} == {"replay1"}

    def test_list_event_items_filters_by_status(self, repo: RunHistoryRepository) -> None:
        repo.create_event_item(
            item_id="a",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="low",
        )
        repo.create_event_item(
            item_id="b",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="low",
        )
        repo.update_event_item("a", {"status": "updated"})

        updated_items = repo.list_event_items(statuses=["updated"])
        assert {i.state.item_id for i in updated_items} == {"a"}

        new_items = repo.list_event_items(statuses=["new"])
        assert {i.state.item_id for i in new_items} == {"b"}

    def test_list_event_items_orders_by_last_reported_desc(
        self, repo: RunHistoryRepository
    ) -> None:
        older = _NOW - timedelta(days=1)
        newer = _NOW
        repo.create_event_item(
            item_id="a",
            origin="live",
            first_reported_at=older,
            last_reported_at=older,
            importance="low",
        )
        repo.create_event_item(
            item_id="b",
            origin="live",
            first_reported_at=newer,
            last_reported_at=newer,
            importance="low",
        )

        items = repo.list_event_items()
        assert [i.state.item_id for i in items] == ["b", "a"]

    def test_update_event_item_ignores_non_allowlisted_fields(
        self, repo: RunHistoryRepository
    ) -> None:
        repo.create_event_item(
            item_id="evt1",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="low",
        )
        updated = repo.update_event_item("evt1", {"id": "hacked", "status": "reinforced"})
        assert updated == 1
        rec = repo.get_event_item("evt1")
        assert rec is not None
        assert rec.state.item_id == "evt1"  # id は allowlist 外 → 変わらない
        assert rec.state.status == "reinforced"

    def test_update_event_item_no_allowlisted_fields_returns_zero(
        self, repo: RunHistoryRepository
    ) -> None:
        repo.create_event_item(
            item_id="evt1",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="low",
        )
        assert repo.update_event_item("evt1", {"id": "hacked"}) == 0

    def test_update_event_item_converts_datetime_values(self, repo: RunHistoryRepository) -> None:
        repo.create_event_item(
            item_id="evt1",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="low",
        )
        later = _NOW + timedelta(hours=3)
        repo.update_event_item("evt1", {"last_reported_at": later})
        rec = repo.get_event_item("evt1")
        assert rec is not None
        assert abs((rec.state.last_reported_at - later).total_seconds()) < 1


class TestEventMembers:
    def test_add_and_list_members(self, repo: RunHistoryRepository) -> None:
        repo.create_event_item(
            item_id="evt1",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="high",
        )
        repo.add_event_member(
            item_id="evt1",
            article_id="a1",
            joined_at=_NOW,
            contributed_new_facts=1,
            join_signal="seed",
        )
        repo.add_event_member(
            item_id="evt1",
            article_id="a2",
            joined_at=_NOW + timedelta(hours=1),
            contributed_new_facts=0,
            join_signal="cve+cos0.81",
        )

        members = repo.list_event_members("evt1")
        assert [m.article_id for m in members] == ["a1", "a2"]
        assert members[0].contributed_new_facts == 1
        assert members[1].join_signal == "cve+cos0.81"

    def test_add_event_member_is_idempotent(self, repo: RunHistoryRepository) -> None:
        repo.create_event_item(
            item_id="evt1",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="high",
        )
        repo.add_event_member(
            item_id="evt1",
            article_id="a1",
            joined_at=_NOW,
            contributed_new_facts=1,
            join_signal="seed",
        )
        # 二重追加 (リプレイの再適用等) は無害 — 最初の値が残る
        repo.add_event_member(
            item_id="evt1",
            article_id="a1",
            joined_at=_NOW,
            contributed_new_facts=0,
            join_signal="dup",
        )

        members = repo.list_event_members("evt1")
        assert len(members) == 1
        assert members[0].contributed_new_facts == 1
        assert members[0].join_signal == "seed"

    def test_get_event_item_includes_member_ids(self, repo: RunHistoryRepository) -> None:
        repo.create_event_item(
            item_id="evt1",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="high",
        )
        repo.add_event_member(
            item_id="evt1",
            article_id="a1",
            joined_at=_NOW,
            contributed_new_facts=1,
            join_signal="seed",
        )
        repo.add_event_member(
            item_id="evt1",
            article_id="a2",
            joined_at=_NOW,
            contributed_new_facts=0,
            join_signal="x",
        )

        rec = repo.get_event_item("evt1")
        assert rec is not None
        assert set(rec.state.member_ids) == {"a1", "a2"}

    def test_list_event_items_batches_member_lookup(self, repo: RunHistoryRepository) -> None:
        repo.create_event_item(
            item_id="a",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="low",
        )
        repo.create_event_item(
            item_id="b",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="low",
        )
        repo.add_event_member(
            item_id="a", article_id="x1", joined_at=_NOW, contributed_new_facts=0, join_signal="s"
        )
        repo.add_event_member(
            item_id="b", article_id="x2", joined_at=_NOW, contributed_new_facts=0, join_signal="s"
        )

        items = {i.state.item_id: i for i in repo.list_event_items()}
        assert items["a"].state.member_ids == ("x1",)
        assert items["b"].state.member_ids == ("x2",)


class TestEventVersions:
    def test_record_and_list_versions(self, repo: RunHistoryRepository) -> None:
        repo.create_event_item(
            item_id="evt1",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="high",
        )
        repo.record_event_version(
            item_id="evt1",
            version=1,
            generated_at=_NOW,
            model="m1",
            prompt_version="v1",
            headline="h1",
            body_json="{}",
            new_facts_json="{}",
            verified_at=_NOW,
            dropped_lines=0,
            repaired_ids=0,
        )
        versions = repo.list_event_versions("evt1")
        assert len(versions) == 1
        assert versions[0].version == 1
        assert versions[0].headline == "h1"
        assert versions[0].verified_at is not None

    def test_record_event_version_upserts_same_version(self, repo: RunHistoryRepository) -> None:
        repo.create_event_item(
            item_id="evt1",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="high",
        )
        repo.record_event_version(
            item_id="evt1",
            version=1,
            generated_at=_NOW,
            model="m1",
            prompt_version="v1",
            headline="first",
            body_json="{}",
            new_facts_json="{}",
            verified_at=None,
            dropped_lines=0,
            repaired_ids=0,
        )
        repo.record_event_version(
            item_id="evt1",
            version=1,
            generated_at=_NOW + timedelta(minutes=5),
            model="m2",
            prompt_version="v2",
            headline="retry",
            body_json="{}",
            new_facts_json="{}",
            verified_at=None,
            dropped_lines=1,
            repaired_ids=1,
        )

        versions = repo.list_event_versions("evt1")
        assert len(versions) == 1  # 生成リトライは上書き (冪等)
        assert versions[0].headline == "retry"
        assert versions[0].model == "m2"
        assert versions[0].dropped_lines == 1

    def test_version_cap_keeps_version_one_and_prunes_oldest(
        self, repo: RunHistoryRepository
    ) -> None:
        """保持上限 20、ただし version=1 は常に保持する (§6)。"""
        repo.create_event_item(
            item_id="evt1",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="high",
        )
        for v in range(1, 22):  # 21 版投入 (cap=20 を 1 件超過)
            repo.record_event_version(
                item_id="evt1",
                version=v,
                generated_at=_NOW + timedelta(minutes=v),
                model="m",
                prompt_version="v1",
                headline=f"h{v}",
                body_json="{}",
                new_facts_json="{}",
                verified_at=None,
                dropped_lines=0,
                repaired_ids=0,
            )
        version_numbers = sorted(v.version for v in repo.list_event_versions("evt1"))
        assert len(version_numbers) == 20
        assert version_numbers[0] == 1  # 初版は常に保持
        assert 2 not in version_numbers  # 超過分は version=1 を除く最古から削除
        assert version_numbers == [1, *range(3, 22)]

    def test_versions_for_missing_item_is_empty(self, repo: RunHistoryRepository) -> None:
        assert repo.list_event_versions("nope") == []


class TestSemanticSkips:
    def test_record_semantic_skips(self, repo: RunHistoryRepository) -> None:
        n = repo.record_semantic_skips([_skip(tier="hard"), _skip(tier="cluster")])
        assert n == 2

    def test_record_semantic_skips_empty_input(self, repo: RunHistoryRepository) -> None:
        assert repo.record_semantic_skips([]) == 0

    def test_record_semantic_skips_rejects_non_semantic_skip(
        self, repo: RunHistoryRepository
    ) -> None:
        with pytest.raises(TypeError):
            repo.record_semantic_skips([{"not": "a SemanticSkip"}])

    def test_purge_semantic_skips_keeps_recent_rows(self, repo: RunHistoryRepository) -> None:
        repo.record_semantic_skips([_skip()])
        assert repo.purge_semantic_skips(days=90) == 0

    def test_purge_semantic_skips_removes_old_rows(self, repo: RunHistoryRepository) -> None:
        repo.record_semantic_skips([_skip()])
        # ts を強制的に古くする (repo に古い ts を書く公開 API が無いため直接 SQL)
        with repo._connect() as conn:  # noqa: SLF001 — 他の repo テストと同型
            conn.execute("UPDATE dedup_semantic_skips SET ts = datetime('now', '-100 days')")
        assert repo.purge_semantic_skips(days=90) == 1
