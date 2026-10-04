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

    def test_list_event_items_filters_by_min_independent_sources(
        self, repo: RunHistoryRepository
    ) -> None:
        """独立媒体数の下限で絞れる — 「複数媒体が報じた事象だけ読む」導線の本体。

        単独報が全体の 9 割を占めるため、既存 facet (カテゴリ等) のどれよりも
        母集団を大きく変える軸になる。
        """
        for item_id, sources in (("solo", 1), ("pair", 2), ("many", 5)):
            repo.create_event_item(
                item_id=item_id,
                origin="live",
                first_reported_at=_NOW,
                last_reported_at=_NOW,
                importance="low",
            )
            repo.update_event_item(item_id, {"independent_sources": sources})

        assert {i.state.item_id for i in repo.list_event_items(min_independent_sources=2)} == {
            "pair",
            "many",
        }
        # 0 は「絞らない」— 未指定と同じ挙動 (UI の「すべて」がここに落ちる)
        assert len(repo.list_event_items(min_independent_sources=0)) == 3

    def test_list_event_items_filters_by_has_news(self, repo: RunHistoryRepository) -> None:
        """生成済み (current_version > 0) の有無で絞れる。

        既定は None = 絞らない。単独記事を一覧から落とさないため
        (docs/event_news_design.md §14b 案 A) 既定値を変えてはいけない。
        """
        for item_id, version in (("generated", 2), ("raw", 0)):
            repo.create_event_item(
                item_id=item_id,
                origin="live",
                first_reported_at=_NOW,
                last_reported_at=_NOW,
                importance="low",
            )
            repo.update_event_item(item_id, {"current_version": version})

        assert {i.state.item_id for i in repo.list_event_items(has_news=True)} == {"generated"}
        assert {i.state.item_id for i in repo.list_event_items(has_news=False)} == {"raw"}
        assert len(repo.list_event_items()) == 2

    def test_list_event_items_can_exclude_duplicate_only_items(
        self, repo: RunHistoryRepository
    ) -> None:
        """全メンバーが dedup で重複判定 かつ 生成本文なし の事象を落とす (公開面用)。

        **LIMIT より前**に効く必要がある — 取得後に間引くと 1 ページの件数が欠ける。
        """
        now = _NOW.isoformat()
        with repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO runs (id, started_at, pipeline, dry_run, status)"
                " VALUES (1, ?, 'eventnews', 0, 'done')",
                (now,),
            )
            for aid, status in (("art-dup", "skipped_duplicate"), ("art-posted", "posted")):
                conn.execute(
                    "INSERT INTO articles (run_id, article_id, url, title, status, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (1, aid, f"https://kuebiko.example/{aid}", aid, status, now),
                )
            conn.commit()

        for item_id, aid in (("dup-only", "art-dup"), ("has-posted", "art-posted")):
            repo.create_event_item(
                item_id=item_id,
                origin="live",
                first_reported_at=_NOW,
                last_reported_at=_NOW,
                importance="high",
            )
            repo.add_event_member(
                item_id=item_id,
                article_id=aid,
                joined_at=_NOW,
                contributed_new_facts=0,
                join_signal="seed",
            )
        # 重複のみだが生成済み = 読み物になっているので残す
        repo.create_event_item(
            item_id="dup-but-generated",
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="high",
        )
        repo.add_event_member(
            item_id="dup-but-generated",
            article_id="art-dup",
            joined_at=_NOW,
            contributed_new_facts=0,
            join_signal="seed",
        )
        repo.update_event_item("dup-but-generated", {"current_version": 1})

        kept = {i.state.item_id for i in repo.list_event_items(exclude_duplicate_only=True)}

        assert "dup-only" not in kept
        assert {"has-posted", "dup-but-generated"} <= kept
        assert len(repo.list_event_items()) == 3  # 既定は落とさない

    def test_list_event_items_can_order_by_corroboration(self, repo: RunHistoryRepository) -> None:
        """独立媒体数の多い順に並べられる (公開面の「注目」枠)。

        ⚠ 並べ替えにだけ使う。重要性は PIR → importance が決める。
        """
        older = _NOW - timedelta(hours=10)
        for item_id, sources, ts in (("few-new", 2, _NOW), ("many-old", 9, older)):
            repo.create_event_item(
                item_id=item_id,
                origin="live",
                first_reported_at=ts,
                last_reported_at=ts,
                importance="high",
            )
            repo.update_event_item(item_id, {"independent_sources": sources})

        by_recency = repo.list_event_items()
        assert [i.state.item_id for i in by_recency] == ["few-new", "many-old"]

        by_corroboration = repo.list_event_items(order_by="corroboration")
        assert [i.state.item_id for i in by_corroboration] == ["many-old", "few-new"]

    def test_list_event_items_can_order_by_level(self, repo: RunHistoryRepository) -> None:
        """重要度 6 段階 (importance_v2) の高い順 (低い level から) に並べられる。

        事象の level = 構成記事のうち最良 (最小) の level。未記録の事象は最後。
        """
        from src.cti.importance_v2 import ImportanceV2

        def _save(article_id: str, severity: str | None, relevant: bool) -> None:
            repo.save_importance_v2(
                article_id,
                ImportanceV2(
                    severity=severity,  # type: ignore[arg-type]
                    severity_basis="x",
                    strategic_weight=None,
                    jp="none",
                    nations=(),
                    sir_ids=(),
                    relevant=relevant,
                ),
            )

        older = _NOW - timedelta(hours=10)
        for item_id, ts in (
            ("worst-new", _NOW),
            ("best-old", older),
            ("unrecorded", older - timedelta(hours=1)),
        ):
            repo.create_event_item(
                item_id=item_id,
                origin="live",
                first_reported_at=ts,
                last_reported_at=ts,
                importance="high",
            )
        repo.add_event_member(
            item_id="worst-new",
            article_id="art-worst",
            joined_at=_NOW,
            contributed_new_facts=0,
            join_signal="seed",
        )
        repo.add_event_member(
            item_id="best-old",
            article_id="art-best",
            joined_at=older,
            contributed_new_facts=0,
            join_signal="seed",
        )
        repo.add_event_member(
            item_id="unrecorded",
            article_id="art-none",
            joined_at=older,
            contributed_new_facts=0,
            join_signal="seed",
        )
        _save("art-worst", "S1", relevant=False)  # level 6
        _save("art-best", "S3", relevant=True)  # level 1
        # art-none は記録なし → level None

        by_recency = repo.list_event_items()
        assert [i.state.item_id for i in by_recency] == ["worst-new", "best-old", "unrecorded"]

        by_level = repo.list_event_items(order_by="level")
        assert [i.state.item_id for i in by_level] == ["best-old", "worst-new", "unrecorded"]

    def test_list_event_items_level_filter(self, repo: RunHistoryRepository) -> None:
        """``level_filter`` — 事象は構成記事のうち最良 (最小) の level で判定 (2026-10-04)。

        "top"=level 1-2 (S3) / "notable"=level 1-4 (S3・S2) または軸なしで
        strategic_weight='heavy' を含む / "relevant"=level 1,3,5。
        """
        from src.cti.importance_v2 import ImportanceV2

        def _save(
            article_id: str,
            severity: str | None,
            relevant: bool,
            strategic_weight: str | None = None,
        ) -> None:
            repo.save_importance_v2(
                article_id,
                ImportanceV2(
                    severity=severity,  # type: ignore[arg-type]
                    severity_basis="x",
                    strategic_weight=strategic_weight,  # type: ignore[arg-type]
                    jp="none",
                    nations=(),
                    sir_ids=(),
                    relevant=relevant,
                ),
            )

        for item_id in (
            "top-event",
            "notable-event",
            "relevant-event",
            "heavy-event",
            "none-event",
        ):
            repo.create_event_item(
                item_id=item_id,
                origin="live",
                first_reported_at=_NOW,
                last_reported_at=_NOW,
                importance="high",
            )
            repo.add_event_member(
                item_id=item_id,
                article_id=f"art-{item_id}",
                joined_at=_NOW,
                contributed_new_facts=0,
                join_signal="seed",
            )

        _save("art-top-event", "S3", relevant=True)  # level 1
        _save("art-notable-event", "S2", relevant=False)  # level 4
        _save("art-relevant-event", "S1", relevant=True)  # level 5
        _save("art-heavy-event", None, relevant=False, strategic_weight="heavy")  # no level
        _save("art-none-event", "S1", relevant=False)  # level 6
        # "none-event" の member は記録無しに近いが level 6 なので top/notable/relevant 全て外れる

        top = {i.state.item_id for i in repo.list_event_items(level_filter="top")}
        assert top == {"top-event"}

        notable = {i.state.item_id for i in repo.list_event_items(level_filter="notable")}
        assert notable == {"top-event", "notable-event", "heavy-event"}

        relevant = {i.state.item_id for i in repo.list_event_items(level_filter="relevant")}
        assert relevant == {"top-event", "relevant-event"}

        assert len(repo.list_event_items()) == 5  # 未指定は絞り込まない

    def test_list_event_items_filters_by_since(self, repo: RunHistoryRepository) -> None:
        """事象そのものの新しさで絞れる (記事側の since_hours とは別物)。"""
        for item_id, ts in (("recent", _NOW), ("stale", _NOW - timedelta(days=5))):
            repo.create_event_item(
                item_id=item_id,
                origin="live",
                first_reported_at=ts,
                last_reported_at=ts,
                importance="high",
            )

        kept = {i.state.item_id for i in repo.list_event_items(since=_NOW - timedelta(hours=72))}
        assert kept == {"recent"}
        assert len(repo.list_event_items()) == 2

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


class TestEventUpdateMarks:
    """続報バッジの材料 — 「本文が書き直された」と「遡及再生成」を混ぜない。"""

    def _item(self, repo: RunHistoryRepository, item_id: str) -> None:
        repo.create_event_item(
            item_id=item_id,
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="high",
        )

    def _version(
        self, repo: RunHistoryRepository, item_id: str, version: int, new_facts: str
    ) -> None:
        repo.record_event_version(
            item_id=item_id,
            version=version,
            generated_at=_NOW,
            model="m",
            prompt_version="v6",
            headline="h",
            body_json="{}",
            new_facts_json=new_facts,
            verified_at=_NOW,
            dropped_lines=0,
            repaired_ids=0,
        )

    def test_only_versions_with_new_facts_count_as_updates(
        self, repo: RunHistoryRepository
    ) -> None:
        # Arrange — 合流で書き直した事象と、プロンプト改訂で作り直しただけの事象
        self._item(repo, "merged")
        self._version(repo, "merged", 1, "[]")
        self._version(repo, "merged", 2, '{"article_id": "a2"}')
        self._item(repo, "regenerated")
        self._version(repo, "regenerated", 1, "[]")
        self._version(repo, "regenerated", 2, "[]")

        # Act
        marks = repo.event_update_marks(["merged", "regenerated"])

        # Assert — 遡及再生成を「更新」と呼ぶと、中身が変わっていない項目が並ぶ
        assert marks["merged"].rewritten_at is not None
        assert marks["merged"].rewrite_count == 1
        assert "regenerated" not in marks

    def test_later_arrivals_without_new_facts_count_as_sources(
        self, repo: RunHistoryRepository
    ) -> None:
        # Arrange — founding member は必ず contributed_new_facts=1 で入る
        self._item(repo, "evt")
        repo.add_event_member(
            item_id="evt", article_id="a1", joined_at=_NOW, contributed_new_facts=1, join_signal=""
        )
        repo.add_event_member(
            item_id="evt", article_id="a2", joined_at=_NOW, contributed_new_facts=0, join_signal=""
        )

        # Act
        mark = repo.event_update_marks(["evt"])["evt"]

        # Assert
        assert mark.follow_up_sources == 1
        assert mark.rewritten_at is None

    def test_items_without_follow_up_are_absent(self, repo: RunHistoryRepository) -> None:
        # Arrange
        self._item(repo, "solo")
        self._version(repo, "solo", 1, "[]")

        # Act / Assert — 初報のままの事象にバッジは出さない
        assert repo.event_update_marks(["solo"]) == {}
        assert repo.event_update_marks([]) == {}


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


class TestLatestVersionsBulk:
    """一覧の N+1 回避 — 複数アイテムの最新版を 1 クエリで返す。"""

    def test_returns_only_the_latest_version_per_item(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(tmp_path / "t.db")
        now = datetime.now(UTC)
        for item_id in ("ev-a", "ev-b"):
            repo.create_event_item(
                item_id=item_id,
                origin="live",
                first_reported_at=now,
                last_reported_at=now,
                importance="high",
            )
        for v in (1, 2, 3):
            repo.record_event_version(
                item_id="ev-a",
                version=v,
                generated_at=now,
                model="m",
                prompt_version="p",
                headline=f"a-v{v}",
                body_json="{}",
                new_facts_json="{}",
                verified_at=None,
                dropped_lines=0,
                repaired_ids=0,
            )
        repo.record_event_version(
            item_id="ev-b",
            version=1,
            generated_at=now,
            model="m",
            prompt_version="p",
            headline="b-v1",
            body_json="{}",
            new_facts_json="{}",
            verified_at=None,
            dropped_lines=0,
            repaired_ids=0,
        )

        latest = repo.latest_event_versions(["ev-a", "ev-b", "ev-missing"])

        assert latest["ev-a"].headline == "a-v3", "最新版 (最大 version) を返す"
        assert latest["ev-b"].headline == "b-v1"
        assert "ev-missing" not in latest

    def test_empty_input_does_not_query(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(tmp_path / "t.db")
        assert repo.latest_event_versions([]) == {}


class TestMoveEventMember:
    """遡及分割の書込 seam。行を消して作り直さず joined_at 等を保つ。"""

    def test_moves_the_member_between_items(self, repo: RunHistoryRepository) -> None:
        # Arrange
        now = datetime(2026, 9, 2, tzinfo=UTC)
        for iid in ("ev-src", "ev-dst"):
            repo.create_event_item(
                item_id=iid,
                origin="live",
                first_reported_at=now,
                last_reported_at=now,
                importance="high",
            )
        repo.add_event_member(
            item_id="ev-src",
            article_id="art-1",
            joined_at=now,
            contributed_new_facts=1,
            join_signal="entity+cos",
        )

        # Act
        moved = repo.move_event_member(
            article_id="art-1", from_item="ev-src", to_item="ev-dst", join_signal="retro_split"
        )

        # Assert — 移動先に 1 件、元は空。joined_at と new_facts は保たれる
        assert moved == 1
        assert repo.list_event_members("ev-src") == []
        dst = repo.list_event_members("ev-dst")
        assert [m.article_id for m in dst] == ["art-1"]
        assert dst[0].join_signal == "retro_split"
        assert dst[0].contributed_new_facts == 1

    def test_missing_member_moves_nothing(self, repo: RunHistoryRepository) -> None:
        # Arrange
        now = datetime(2026, 9, 2, tzinfo=UTC)
        repo.create_event_item(
            item_id="ev-a",
            origin="live",
            first_reported_at=now,
            last_reported_at=now,
            importance="high",
        )

        # Act / Assert
        assert (
            repo.move_event_member(
                article_id="ghost", from_item="ev-a", to_item="ev-b", join_signal="retro_split"
            )
            == 0
        )


class TestRelatedEvents:
    """「別事象だが関連」(related_to) の逆引き。分割の一覧性回復に使う。"""

    def test_children_are_listed_and_merged_ones_are_excluded(
        self, repo: RunHistoryRepository
    ) -> None:
        # Arrange — 親 1 + 子 2 (うち 1 つは統合で吸収済み)
        now = datetime(2026, 9, 3, tzinfo=UTC)
        repo.create_event_item(
            item_id="ev-parent",
            origin="live",
            first_reported_at=now,
            last_reported_at=now,
            importance="high",
        )
        for iid in ("ev-kid1", "ev-kid2"):
            repo.create_event_item(
                item_id=iid,
                origin="live",
                first_reported_at=now,
                last_reported_at=now,
                importance="medium",
                related_to="ev-parent",
            )
        repo.update_event_item("ev-kid2", {"merged_into": "ev-parent"})

        # Act
        got = repo.list_related_events("ev-parent")

        # Assert — 生存している子だけ
        assert [r.state.item_id for r in got] == ["ev-kid1"]
        assert got[0].related_to == "ev-parent"

    def test_no_relations_returns_empty(self, repo: RunHistoryRepository) -> None:
        assert repo.list_related_events("ev-nothing") == []


class TestDraftRejects:
    """DPO の rejected 側の蛇口。採用版と対で読めることが契約。"""

    def test_record_and_list(self, repo: RunHistoryRepository) -> None:
        # Arrange / Act
        repo.record_draft_reject(
            item_id="ev-a", version=2, model="m", hints="逐語", draft_json='{"facts":[]}'
        )

        # Assert
        got = repo.list_draft_rejects()
        assert len(got) == 1
        assert got[0]["item_id"] == "ev-a" and got[0]["version"] == 2
        assert got[0]["hints"] == "逐語"


def test_event_item_ids_by_article_maps_members_to_their_items(repo: RunHistoryRepository) -> None:
    """記事 → 属する事象 (detect の束ねの判定に使う、2026-09-24)。"""
    for item_id, aids in (("e1", ["a1", "a2"]), ("e2", ["a2", "a3"])):
        repo.create_event_item(
            item_id=item_id,
            origin="live",
            first_reported_at=_NOW,
            last_reported_at=_NOW,
            importance="high",
        )
        for aid in aids:
            repo.add_event_member(
                item_id=item_id,
                article_id=aid,
                joined_at=_NOW,
                contributed_new_facts=0,
                join_signal="seed",
            )

    got = repo.event_item_ids_by_article(["a1", "a2", "a3", "none"])

    assert got == {"a1": {"e1"}, "a2": {"e1", "e2"}, "a3": {"e2"}}
