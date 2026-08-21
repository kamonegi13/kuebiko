"""較正格子 P1 (self_evolving_tuning_design §6): tuning_labels repo + 遅延正解収穫のテスト。

ラベルは「LLM 出力の採点」でなく「外部正解との突合」を起点に定義する (設計 §2)。
収穫は週次 sweep (pull 型・冪等) — 人間が一切触らなくてもラベルが増え続けること (§12) を
dedup_key の冪等性テストで固定する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository
from src.tuning.label_harvest import run_label_harvest

_NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "tuning.db")


def _seed_article(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    subject_actor_source: str = "none",
    subject_actor_ids: str = "",
    body_len: int = 600,
    article_type: str = "",
    victim_orgs: tuple[str, ...] = (),
    created_at: datetime = _NOW,
) -> None:
    """収穫の入力 (articles + article_entities) を直接 seed する (test 専用)。"""
    iso = created_at.isoformat()
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (started_at, pipeline, dry_run, status) VALUES (?, 't', 0, 'done')",
            (iso,),
        )
        rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (run_id, article_id, title, url, status, created_at,"
            " subject_actor_source, subject_actor_ids, body, article_type)"
            " VALUES (?,?,?,?, 'posted', ?, ?, ?, ?, ?)",
            (
                rid,
                article_id,
                f"title-{article_id}",
                f"https://kuebiko.example/{article_id}",
                iso,
                subject_actor_source,
                subject_actor_ids,
                "x" * body_len,
                article_type,
            ),
        )
        for org in victim_orgs:
            conn.execute(
                "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
                " VALUES (?,?,?,?)",
                (article_id, "victim_org", org, iso),
            )


class TestTuningLabelRepo:
    def test_record_and_summarize(self, repo: RunHistoryRepository) -> None:
        # Arrange / Act
        first = repo.record_tuning_label(
            dedup_key="k1",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength="strong",
            provenance=json.dumps({"producer": "test"}),
            article_id="a1",
        )
        repo.record_tuning_label(
            dedup_key="k2",
            field="editorial_stance",
            label_value="propaganda",
            source="E3",
            strength="strong",
            provenance="{}",
            article_id="a2",
        )
        # Assert
        assert first is not None
        rows = repo.summarize_tuning_labels()
        by_key = {(r["field"], r["source"]): r for r in rows}
        assert by_key[("subject_actor", "E1")]["active"] == 1
        assert by_key[("editorial_stance", "E3")]["active"] == 1

    def test_dedup_key_is_idempotent(self, repo: RunHistoryRepository) -> None:
        def _record() -> int | None:
            return repo.record_tuning_label(
                dedup_key="same",
                field="subject_actor",
                label_value="qilin",
                source="E1",
                strength="strong",
                provenance="{}",
            )

        assert _record() is not None
        assert _record() is None  # 既出 → 重複しない
        rows = repo.summarize_tuning_labels()
        assert rows[0]["total"] == 1

    def test_supersede_same_article_field_source(self, repo: RunHistoryRepository) -> None:
        old_id = repo.record_tuning_label(
            dedup_key="e1",
            field="editorial_stance",
            label_value="opinion",
            source="E3",
            strength="strong",
            provenance="{}",
            article_id="a1",
            supersede_same=True,
        )
        new_id = repo.record_tuning_label(
            dedup_key="e2",
            field="editorial_stance",
            label_value="propaganda",
            source="E3",
            strength="strong",
            provenance="{}",
            article_id="a1",
            supersede_same=True,
        )
        assert old_id is not None and new_id is not None
        rows = repo.list_tuning_labels(field="editorial_stance")
        by_id = {r["id"]: r for r in rows}
        assert by_id[old_id]["superseded_by"] == new_id
        assert by_id[new_id]["superseded_by"] is None
        # summarize は現行 (active) と総数 (total) を区別する
        summary = repo.summarize_tuning_labels()
        row = next(r for r in summary if r["field"] == "editorial_stance")
        assert row["active"] == 1
        assert row["total"] == 2

    def test_supersede_does_not_cross_articles(self, repo: RunHistoryRepository) -> None:
        a = repo.record_tuning_label(
            dedup_key="x1",
            field="subject_actor",
            label_value="qilin",
            source="E1",
            strength="strong",
            provenance="{}",
            article_id="a1",
            supersede_same=True,
        )
        repo.record_tuning_label(
            dedup_key="x2",
            field="subject_actor",
            label_value="akira",
            source="E1",
            strength="strong",
            provenance="{}",
            article_id="a2",
            supersede_same=True,
        )
        rows = repo.list_tuning_labels(field="subject_actor")
        assert all(r["superseded_by"] is None for r in rows)
        assert a is not None


class TestFeedNewsSubjectSweep:
    def test_matches_claim_and_news_within_window(self, repo: RunHistoryRepository) -> None:
        # Arrange: 犯行声明 (feed 帰属) と、同一 victim_org を 1 日後に報じたニュース
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("acme corp",),  # 大文字小文字ゆれは吸収される
            created_at=_NOW - timedelta(days=1),
        )
        # Act
        result = run_label_harvest(repo, now=_NOW)
        # Assert
        assert result.feed_subject_new == 1
        rows = repo.list_tuning_labels(field="subject_actor")
        assert len(rows) == 1
        assert rows[0]["article_id"] == "news1"
        assert rows[0]["label_value"] == "qilin"
        assert rows[0]["source"] == "E1"

    def test_outside_window_is_not_matched(self, repo: RunHistoryRepository) -> None:
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=20),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.feed_subject_new == 0

    def test_conflicting_claims_are_skipped(self, repo: RunHistoryRepository) -> None:
        # 同一 org に別ギャングの声明が窓内に 2 つ → 正解を決められないので付与しない
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="claim2",
            subject_actor_source="feed",
            subject_actor_ids="akira",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=3),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.feed_subject_new == 0
        assert result.feed_subject_conflicts == 1

    def test_short_body_and_recap_are_excluded(self, repo: RunHistoryRepository) -> None:
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="stub",
            body_len=100,  # 500 字未満は判定材料にならない
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        _seed_article(
            repo,
            article_id="recap1",
            article_type="recap",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.feed_subject_new == 0

    def test_sweep_is_idempotent(self, repo: RunHistoryRepository) -> None:
        _seed_article(
            repo,
            article_id="claim1",
            subject_actor_source="feed",
            subject_actor_ids="qilin",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=2),
        )
        _seed_article(
            repo,
            article_id="news1",
            victim_orgs=("Acme Corp",),
            created_at=_NOW - timedelta(days=1),
        )
        first = run_label_harvest(repo, now=_NOW)
        second = run_label_harvest(repo, now=_NOW)
        assert first.feed_subject_new == 1
        assert second.feed_subject_new == 0  # 再実行で増えない
        assert len(repo.list_tuning_labels(field="subject_actor")) == 1


class TestE3Sweeps:
    def test_taxonomy_decisions_become_labels(self, repo: RunHistoryRepository) -> None:
        from src.storage.records import TaxonomyProposalRecord

        pid = repo.insert_taxonomy_proposal(
            TaxonomyProposalRecord(
                proposal_type="pattern_4",
                tier="tier_1_auto",
                target_yaml="victim_sectors",
                target_canonical="energy",
                proposed_change='{"kind": "add_alias"}',
                rationale="typo",
                confidence="high",
            )
        )
        repo.update_taxonomy_proposal_status(pid, status="accepted")
        # pending の提案はラベルにならない
        repo.insert_taxonomy_proposal(
            TaxonomyProposalRecord(
                proposal_type="pattern_5",
                tier="tier_2_review",
                target_yaml="actor_aliases",
                target_canonical="foo",
                proposed_change='{"kind": "new_actor"}',
                rationale="x",
                confidence="low",
            )
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.taxonomy_new == 1
        rows = repo.list_tuning_labels(field="taxonomy_decision")
        assert len(rows) == 1
        assert rows[0]["label_value"] == "accepted"
        assert rows[0]["source"] == "E3"
        # 冪等
        assert run_label_harvest(repo, now=_NOW).taxonomy_new == 0

    def test_editorial_corrections_become_labels(self, repo: RunHistoryRepository) -> None:
        repo.upsert_editorial_stance_review(
            article_id="a1",
            original_stance="factual_report",
            corrected_stance="propaganda",
            comment="国営メディアの引き写し",
        )
        result = run_label_harvest(repo, now=_NOW)
        assert result.editorial_new == 1
        rows = repo.list_tuning_labels(field="editorial_stance")
        assert len(rows) == 1
        assert rows[0]["article_id"] == "a1"
        assert rows[0]["label_value"] == "propaganda"
        assert rows[0]["source"] == "E3"
        # 自由文コメントは provenance に持ち込まない (§4 マスク方針: 書込側で遮断)
        assert "国営" not in rows[0]["provenance"]

    def test_editorial_recorrection_supersedes(self, repo: RunHistoryRepository) -> None:
        repo.upsert_editorial_stance_review(
            article_id="a1",
            original_stance="factual_report",
            corrected_stance="opinion",
            comment="",
        )
        run_label_harvest(repo, now=_NOW)
        repo.upsert_editorial_stance_review(
            article_id="a1",
            original_stance="factual_report",
            corrected_stance="propaganda",
            comment="",
        )
        run_label_harvest(repo, now=_NOW)
        rows = repo.list_tuning_labels(field="editorial_stance")
        active = [r for r in rows if r["superseded_by"] is None]
        assert len(active) == 1
        assert active[0]["label_value"] == "propaganda"

    def test_dry_run_writes_nothing(self, repo: RunHistoryRepository) -> None:
        repo.upsert_editorial_stance_review(
            article_id="a1",
            original_stance=None,
            corrected_stance="propaganda",
            comment="",
        )
        result = run_label_harvest(repo, now=_NOW, dry_run=True)
        assert result.editorial_new == 1  # 候補数は報告する
        assert repo.list_tuning_labels() == []  # 書き込まない
