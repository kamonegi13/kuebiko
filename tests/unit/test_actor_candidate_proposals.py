"""Actor Recall Layer Part C2/C3: 暗定永続化 → 裏取り集計 → 提案 → backfill。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cti.actor_candidate_proposals import PROPOSAL_TYPE, propose_emerging_actors
from src.main import _persist_article_entities
from src.storage.run_history import RunHistoryRepository
from src.tools.discord_publisher import BriefingMessage


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "arl.db")


def _entities(repo: RunHistoryRepository, article_id: str) -> set[tuple[str, str]]:
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT entity_type, value FROM article_entities WHERE article_id=?",
            (article_id,),
        ).fetchall()
    return {(str(r["entity_type"]), str(r["value"])) for r in rows}


# ---------- C2: 暗定 entity 永続化 ----------


def test_provisional_candidate_persisted_separately_from_actor(repo: RunHistoryRepository) -> None:
    msg = BriefingMessage(
        title="新興クラスタ Storm-9999",
        importance="high",
        category="apt",
        summary="s",
        metadata={
            "detected_actor_ids": ["lazarus"],
            "provisional_actor_candidates": [
                {"key": "storm-9999", "name": "Storm-9999", "signal": "vendor_designation"}
            ],
        },
    )
    _persist_article_entities(dedup_repo=repo, article_id="a1", msg=msg)
    ents = _entities(repo, "a1")
    # 確定 actor と暗定は別 entity_type で共存 (混同しない)
    assert ("actor", "lazarus") in ents
    assert ("actor_provisional", "storm-9999") in ents


# ---------- C2: 裏取り集計 → 提案 ----------


def _seed_provisional(repo: RunHistoryRepository, key: str, article_ids: list[str]) -> None:
    for aid in article_ids:
        repo.add_article_entities(aid, [("actor_provisional", key)])


def test_proposed_when_corroborated_across_min_articles(repo: RunHistoryRepository) -> None:
    _seed_provisional(repo, "storm-9999", ["a1", "a2", "a3"])
    stats = propose_emerging_actors(repo, min_articles=3)
    assert stats["proposed"] == 1
    p = repo.find_actor_update_proposal(proposal_type=PROPOSAL_TYPE, dedup_key="corpus:storm-9999")
    assert p is not None
    payload = json.loads(p.payload)
    assert payload["canonical"] == "Storm-9999"  # ベンダ designation の表示名復元
    assert payload["_evidence"]["article_count"] == 3


def test_not_proposed_below_threshold(repo: RunHistoryRepository) -> None:
    _seed_provisional(repo, "storm-1", ["a1", "a2"])  # 2 < 3
    stats = propose_emerging_actors(repo, min_articles=3)
    assert stats["proposed"] == 0


def test_known_actor_not_proposed(repo: RunHistoryRepository) -> None:
    # 実 yaml に seed 済みの NoName (key=noname057(16)) は提案しない
    _seed_provisional(repo, "noname057(16)", ["a1", "a2", "a3"])
    stats = propose_emerging_actors(repo, min_articles=3)
    assert stats["proposed"] == 0


def test_slug_variant_of_known_actor_not_proposed(repo: RunHistoryRepository) -> None:
    """既知 actor の綴り違い slug は resolve_source_slug で既知扱い → 提案しない。

    2026-08-01 事故: "thegentlemen" (既存 the_gentlemen の slug 綴り) が knows_name
    (word-boundary 照合) を素通りして提案・採用され id 分裂した。起票段階で
    slug 正規化照合を併用して根から止める。
    """
    _seed_provisional(repo, "volttyphoon", ["a1", "a2", "a3"])
    stats = propose_emerging_actors(repo, min_articles=3)
    assert stats["proposed"] == 0


def test_no_duplicate_proposal(repo: RunHistoryRepository) -> None:
    _seed_provisional(repo, "unc5999", ["a1", "a2", "a3"])
    propose_emerging_actors(repo, min_articles=3)
    stats2 = propose_emerging_actors(repo, min_articles=3)  # 2 回目: dedup_key で skip
    assert stats2["proposed"] == 0
    assert stats2["skipped"] >= 1


def test_rejected_proposal_not_reproposed(repo: RunHistoryRepository) -> None:
    _seed_provisional(repo, "unc5999", ["a1", "a2", "a3"])
    propose_emerging_actors(repo, min_articles=3)
    p = repo.find_actor_update_proposal(proposal_type=PROPOSAL_TYPE, dedup_key="corpus:unc5999")
    assert p is not None
    assert p.id is not None
    repo.decide_actor_update_proposal(p.id, status="rejected")
    stats = propose_emerging_actors(repo, min_articles=3)  # 却下後も再提案しない
    assert stats["proposed"] == 0


# ---------- C3: 承認時 backfill ----------


def test_promote_provisional_backfills_and_clears(repo: RunHistoryRepository) -> None:
    _seed_provisional(repo, "storm-9999", ["a1", "a2"])
    repo.add_article_entities("a1", [("actor", "storm_9999")])  # a1 は既に確定行あり (conflict)
    promoted = repo.promote_provisional_actor("storm-9999", "storm_9999")
    # a2 のみ新規確定 (a1 は既存で IGNORE) → 1
    assert promoted == 1
    assert ("actor", "storm_9999") in _entities(repo, "a2")
    # 暗定行は全削除
    assert ("actor_provisional", "storm-9999") not in _entities(repo, "a1")
    assert ("actor_provisional", "storm-9999") not in _entities(repo, "a2")


class TestGenericWordGateAtProposal:
    """一般語衝突ゲート (2026-07-31): 07-26 バッチ承認で Play 等 11 体が ambiguous なしで
    辞書入りし言及層の 59% が誤検出化した事故の再発防止。起票時に自動付与する。"""

    def test_generic_word_candidate_gets_ambiguous_flag(self, repo: RunHistoryRepository) -> None:
        # "cloud" は GENERIC_ALIAS_WORDS (SSoT) 収録の一般語
        _seed_provisional(repo, "cloud", ["a1", "a2", "a3"])
        propose_emerging_actors(repo, min_articles=3)
        p = repo.find_actor_update_proposal(proposal_type=PROPOSAL_TYPE, dedup_key="corpus:cloud")
        assert p is not None
        payload = json.loads(p.payload)
        assert payload.get("ambiguous") is True
        assert "一般語衝突" in p.rationale

    def test_single_word_non_generic_gets_review_hint(self, repo: RunHistoryRepository) -> None:
        _seed_provisional(repo, "payoutcrew", ["a1", "a2", "a3"])
        propose_emerging_actors(repo, min_articles=3)
        p = repo.find_actor_update_proposal(
            proposal_type=PROPOSAL_TYPE, dedup_key="corpus:payoutcrew"
        )
        assert p is not None
        payload = json.loads(p.payload)
        assert "ambiguous" not in payload  # SSoT 外は自動付与しない (人が判断)
        assert "1 語名" in p.rationale

    def test_multiword_candidate_no_hint(self, repo: RunHistoryRepository) -> None:
        _seed_provisional(repo, "acme secret band", ["a1", "a2", "a3"])
        propose_emerging_actors(repo, min_articles=3)
        p = repo.find_actor_update_proposal(
            proposal_type=PROPOSAL_TYPE, dedup_key="corpus:acme secret band"
        )
        assert p is not None
        assert "1 語名" not in p.rationale
        assert "一般語衝突" not in p.rationale


def test_uat_designation_display_and_signal(repo: RunHistoryRepository) -> None:
    """2026-08-01 pattern_5 退役: UAT key はベンダ designation として表示名復元される。"""
    _seed_provisional(repo, "uat-7811", ["a1", "a2", "a3"])
    stats = propose_emerging_actors(repo, min_articles=3)
    assert stats["proposed"] == 1
    p = repo.find_actor_update_proposal(proposal_type=PROPOSAL_TYPE, dedup_key="corpus:uat-7811")
    assert p is not None
    payload = json.loads(p.payload)
    assert payload["canonical"] == "UAT-7811"
    assert payload["_evidence"]["signal"] == "vendor_designation"
