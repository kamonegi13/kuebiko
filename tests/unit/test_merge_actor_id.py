"""merge_actor_id script (actor id merge の過去データ remap) のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.merge_actor_id import _remap_csv, _run
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


class TestRemapCsv:
    def test_replaces_and_dedupes(self) -> None:
        # 二重付与 (the_gentlemen,thegentlemen) → 1 トークンに畳む
        assert _remap_csv("the_gentlemen,thegentlemen", "thegentlemen", "the_gentlemen") == (
            "the_gentlemen"
        )

    def test_replaces_single_token(self) -> None:
        assert _remap_csv("thegentlemen", "thegentlemen", "the_gentlemen") == "the_gentlemen"

    def test_preserves_other_tokens_and_order(self) -> None:
        assert _remap_csv("qilin,thegentlemen,akira_ransom", "thegentlemen", "the_gentlemen") == (
            "qilin,the_gentlemen,akira_ransom"
        )

    def test_untouched_when_absent(self) -> None:
        assert _remap_csv("qilin", "thegentlemen", "the_gentlemen") == "qilin"


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "merge.db")


def _seed(repo: RunHistoryRepository, article_id: str, subject_ids: str) -> None:
    run_id = repo.start_run(
        RunRecord(started_at=datetime.now(UTC), pipeline="daily", dry_run=False)
    )
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title="t",
            url=f"https://e/{article_id}",
            status="posted",
            subject_actor_ids=subject_ids,
        ),
    )


class TestRunRemap:
    """実辞書 (thegentlemen 墓標 → the_gentlemen) を前提にした remap フロー。"""

    def test_apply_remaps_subject_and_entities(self, repo: RunHistoryRepository) -> None:
        # Arrange: 二重付与 1 件 + 単独付与 1 件 + 無関係 1 件
        _seed(repo, "dual", "the_gentlemen,thegentlemen")
        _seed(repo, "solo", "thegentlemen")
        _seed(repo, "other", "qilin")
        # entities: dual は両行あり (dedup DELETE)、solo は旧 id のみ (UPDATE)
        repo.add_article_entities("dual", [("actor", "the_gentlemen"), ("actor", "thegentlemen")])
        repo.add_article_entities("solo", [("actor", "thegentlemen")])

        # Act
        _run("thegentlemen", "the_gentlemen", apply=True, repo=repo)

        # Assert
        with repo._connect() as con:  # noqa: SLF001
            subj = {
                str(r["article_id"]): str(r["subject_actor_ids"])
                for r in con.execute(
                    "SELECT article_id, subject_actor_ids FROM articles"
                ).fetchall()
            }
            assert subj["dual"] == "the_gentlemen"
            assert subj["solo"] == "the_gentlemen"
            assert subj["other"] == "qilin"
            ents = [
                (str(r["article_id"]), str(r["value"]))
                for r in con.execute(
                    "SELECT article_id, value FROM article_entities"
                    " WHERE entity_type='actor' ORDER BY article_id, value"
                ).fetchall()
            ]
            assert ents == [("dual", "the_gentlemen"), ("solo", "the_gentlemen")]

    def test_dry_run_changes_nothing(self, repo: RunHistoryRepository) -> None:
        _seed(repo, "solo", "thegentlemen")
        repo.add_article_entities("solo", [("actor", "thegentlemen")])

        _run("thegentlemen", "the_gentlemen", apply=False, repo=repo)

        with repo._connect() as con:  # noqa: SLF001
            row = con.execute(
                "SELECT subject_actor_ids FROM articles WHERE article_id='solo'"
            ).fetchone()
            assert str(row["subject_actor_ids"]) == "thegentlemen"

    def test_refuses_when_target_missing(self, repo: RunHistoryRepository) -> None:
        with pytest.raises(SystemExit):
            _run("thegentlemen", "no_such_actor_id", apply=False, repo=repo)
