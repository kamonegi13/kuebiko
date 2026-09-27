"""アクター 1 件の STIX 2.1 bundle (2026-09-27) — 主題の記事を束ね、LLM medium の主題は外す。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
from src.cti.stix.actor import build_actor_bundle
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from tests.unit.stix_validation import assert_extensions_match_schema, assert_valid_stix


def _registry() -> ActorAliasRegistry:
    return ActorAliasRegistry(actors=(ActorAlias(id="apt28", canonical="APT28", nation="ru"),))


def _repo(tmp_path: Path) -> RunHistoryRepository:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    for aid, source, conf in (("a1", "title", ""), ("a2", "llm", "medium")):
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=aid,
                title=f"APT28 {aid}",
                url=f"https://kuebiko.example/{aid}",
                status="posted",
                subject_actor_ids="apt28",
                subject_actor_source=source,
                subject_actor_confidence=conf or None,
            )
        )
        repo.add_article_entities(aid, [("actor", "apt28"), ("malware_family", "X-Agent")])
    return repo


def test_actor_bundle_is_valid_and_skips_llm_medium(tmp_path: Path) -> None:
    b = build_actor_bundle(_repo(tmp_path), _registry(), "apt28")

    assert b is not None
    assert_valid_stix(b)
    assert_extensions_match_schema(b)
    reports = [x for x in b["objects"] if x["type"] == "report"]
    assert [r["name"] for r in reports] == ["APT28 a1"]  # a2 (LLM medium) は外れる


def test_unknown_actor_is_none(tmp_path: Path) -> None:
    assert build_actor_bundle(_repo(tmp_path), _registry(), "nobody") is None
