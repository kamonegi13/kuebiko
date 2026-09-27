"""台帳の型 (actor / campaign、2026-09-27) — 判定と、アクター追跡は自動で閉じないこと。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.assessment.ledger import _sweep_lifecycle
from src.assessment.situation_store import SituationStore
from src.assessment.situation_track import TRACK_ACTOR, TRACK_CAMPAIGN, decide_track


def _is_group(actor_id: str) -> bool:
    return actor_id in {"apt28", "lazarus"}


def test_trusted_subject_matching_anchor_makes_actor_track() -> None:
    got = decide_track(
        ["actor:apt28", "cve:CVE-2026-1"], [("apt28", "title", "")], is_group=_is_group
    )

    assert got == TRACK_ACTOR


def test_llm_medium_subject_is_not_enough() -> None:
    got = decide_track(["actor:apt28"], [("apt28", "llm", "medium")], is_group=_is_group)

    assert got == TRACK_CAMPAIGN


def test_llm_high_subject_counts() -> None:
    got = decide_track(["actor:lazarus"], [("lazarus", "llm", "high")], is_group=_is_group)

    assert got == TRACK_ACTOR


def test_organization_anchor_is_campaign() -> None:
    # 機関 (group でない) を軸にした台帳は持続的な侵入の主体ではない
    got = decide_track(["actor:russia_gru"], [("russia_gru", "title", "")], is_group=_is_group)

    assert got == TRACK_CAMPAIGN


def test_no_actor_anchor_is_campaign() -> None:
    got = decide_track(["cve:CVE-2026-1"], [("apt28", "title", "")], is_group=_is_group)

    assert got == TRACK_CAMPAIGN


def test_subject_not_in_anchors_is_campaign() -> None:
    got = decide_track(["actor:apt28"], [("lazarus", "title", "")], is_group=_is_group)

    assert got == TRACK_CAMPAIGN


def _dormant_long_ago(store: SituationStore, title: str, track: str | None) -> str:
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    row = store.open_situation(
        title=title, domain="cyber_incident", anchors=frozenset(), pir_ids=(), now_iso=old
    )
    store.set_status(row.situation_id, "dormant")
    if track is not None:
        store.set_track(row.situation_id, track)
    return row.situation_id


def test_actor_track_is_not_closed_by_lifecycle(tmp_path: Path) -> None:
    store = SituationStore(db_path=tmp_path / "ledger.db")
    actor_sid = _dormant_long_ago(store, "APT28 の活動", TRACK_ACTOR)
    campaign_sid = _dormant_long_ago(store, "ある脆弱性の悪用", TRACK_CAMPAIGN)

    _sweep_lifecycle(store=store, now=datetime.now(UTC))

    status = {r.situation_id: r.status for r in store.load_situations(("dormant", "closed"))}
    assert status[actor_sid] == "dormant"
    assert status[campaign_sid] == "closed"
