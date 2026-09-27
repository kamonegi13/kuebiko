"""台帳 1 件の STIX 2.1 bundle (2026-09-27) — 型ごとの中心オブジェクトと準拠。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.assessment.situation_store import RevisionRow, SituationStore
from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
from src.cti.stix.situation import build_situation_bundle
from src.storage.run_history import ArticleRecord, RunRecord
from tests.unit.stix_validation import assert_extensions_match_schema, assert_valid_stix

_NOW = "2026-09-20T00:00:00+00:00"


def _registry() -> ActorAliasRegistry:
    return ActorAliasRegistry(actors=(ActorAlias(id="apt28", canonical="APT28", nation="ru"),))


def _setup(tmp_path: Path, *, track: str) -> tuple[SituationStore, str]:
    store = SituationStore(db_path=tmp_path / "ledger.db")
    repo = store._repo  # noqa: SLF001 — テストからの意図的な内部参照
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id="a1",
            title="APT28 が政府機関を標的に",
            url="https://kuebiko.example/a1",
            status="posted",
            subject_actor_ids="apt28",
            subject_actor_source="title",
        )
    )
    repo.add_article_entities("a1", [("actor", "apt28"), ("cve", "CVE-2026-1234")])
    row = store.open_situation(
        title="APT28 が政府機関への侵入を続けている",
        domain="cyber_incident",
        anchors=frozenset({"actor:apt28"}),
        pir_ids=("pir_russia_apt",),
        now_iso=_NOW,
    )
    sid = row.situation_id
    store.set_track(sid, track)
    store.record_assignment(situation_id=sid, article_id="a1", added_at=_NOW, assigned_by="seed")
    with repo._connect() as conn:  # noqa: SLF001 — ACH 引用済み (評価済み) の状態を直接作る
        conn.execute(
            "UPDATE situation_evidence SET assessed_at=?, polarity='supports' WHERE situation_id=?",
            (_NOW, sid),
        )
    store.add_revision(
        RevisionRow(
            situation_id=sid,
            rev=0,
            claim="APT28 が政府機関への侵入を続けている",
            claim_type="ongoing_activity",
            leading_hypothesis="organized_state_op",
            confidence="moderate",
            confidence_basis="複数の独立した報告",
            hypotheses_json=json.dumps(
                [
                    {
                        "hypothesis": "国家の組織的作戦",
                        "consistent": 3,
                        "inconsistent": 0,
                        "verdict": "leading",
                    }
                ]
            ),
            assumptions_json="[]",
            missing_json='["初期侵入の経路"]',
            indicators_json="[]",
            implication="国内の政府機関も標的になりうる",
            delta_type="opened",
            delta_note="",
            created_at=_NOW,
        )
    )
    return store, sid


def _types(b: dict[str, Any]) -> list[str]:
    return [x["type"] for x in b["objects"]]


def test_campaign_track_exports_campaign_attributed_to_intrusion_set(tmp_path: Path) -> None:
    store, sid = _setup(tmp_path, track="campaign")

    b = build_situation_bundle(store, store._repo, _registry(), sid)  # noqa: SLF001

    assert b is not None
    assert_valid_stix(b)
    assert_extensions_match_schema(b)
    assert "campaign" in _types(b) and "grouping" in _types(b) and "note" in _types(b)
    rels = [x for x in b["objects"] if x.get("relationship_type") == "attributed-to"]
    camp = next(x for x in b["objects"] if x["type"] == "campaign")
    assert any(r["source_ref"] == camp["id"] for r in rels)


def test_actor_track_centers_on_intrusion_set(tmp_path: Path) -> None:
    store, sid = _setup(tmp_path, track="actor")

    b = build_situation_bundle(store, store._repo, _registry(), sid)  # noqa: SLF001

    assert b is not None
    assert_valid_stix(b)
    assert "campaign" not in _types(b)
    grouping = next(x for x in b["objects"] if x["type"] == "grouping")
    intrusion = next(x for x in b["objects"] if x["type"] == "intrusion-set")
    assert intrusion["id"] in grouping["object_refs"]


def test_note_carries_ach_confidence(tmp_path: Path) -> None:
    store, sid = _setup(tmp_path, track="campaign")

    b = build_situation_bundle(store, store._repo, _registry(), sid)  # noqa: SLF001

    assert b is not None
    note = next(x for x in b["objects"] if x["type"] == "note")
    assert note["confidence"] == 50
    assert "国家の組織的作戦" in note["content"]


def test_unknown_situation_is_none(tmp_path: Path) -> None:
    store = SituationStore(db_path=tmp_path / "ledger.db")

    assert build_situation_bundle(store, store._repo, _registry(), "s-none") is None  # noqa: SLF001
