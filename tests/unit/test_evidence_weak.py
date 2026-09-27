"""弱い証拠の印 (2026-09-27) — 評価の読み取りから外れ、割当済みの判定には残ること。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import src.assessment.stateful as stateful
from src.assessment.evidence_weak import scan_weak_evidence
from src.assessment.situation_store import AssignedBy, SituationStore
from src.storage.run_history import ArticleRecord, RunRecord

_NOW = datetime.now(UTC).isoformat()


@pytest.fixture
def store(tmp_path: Path) -> SituationStore:
    st = SituationStore(db_path=tmp_path / "ledger.db")
    repo = st._repo  # noqa: SLF001 — テストからの意図的な内部参照
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    for i in range(1, 6):
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=f"a{i}",
                title=f"t{i}",
                url=f"https://kuebiko.example/a{i}",
                status="posted",
            )
        )
    return st


def _open_with_evidence(store: SituationStore) -> str:
    sit = store.open_situation(
        title="情勢", domain="cyber_incident", anchors=frozenset(), pir_ids=(), now_iso=_NOW
    )
    sid = sit.situation_id
    pairs: tuple[tuple[str, AssignedBy], ...] = (
        ("a1", "seed"),
        ("a2", "anchor"),
        ("a3", "token"),
        ("a4", "nation"),
    )
    for aid, by in pairs:
        store.record_assignment(situation_id=sid, article_id=aid, added_at=_NOW, assigned_by=by)
    return sid


def test_weak_evidence_is_hidden_from_assessment_reads(store: SituationStore) -> None:
    sid = _open_with_evidence(store)

    n = store.mark_weak([(sid, "a3"), (sid, "a4")], weak_at=_NOW)

    assert n == 2
    assert set(store.evidence_article_ids(sid)) == {"a1", "a2"}
    assert set(store.unread_evidence()[sid]) == {"a1", "a2"}
    assert store.evidence_ids_by_situation([sid])[sid] == {"a1", "a2"}
    assert set(store.evidence_ids_added_since(sid, since_iso="2000-01-01")) == {"a1", "a2"}
    assert store.evidence_state_counts([sid])[sid]["total"] == 2
    # 割当済みの判定には残す (同じ記事を別の情勢へ割り当て直さない)
    assert {"a3", "a4"} <= store.assigned_article_ids()


def test_mark_weak_is_idempotent(store: SituationStore) -> None:
    sid = _open_with_evidence(store)
    store.mark_weak([(sid, "a2")], weak_at=_NOW)

    assert store.mark_weak([(sid, "a2")], weak_at="2099-01-01T00:00:00+00:00") == 0


def test_unmarked_evidence_by_rule_skips_seed_and_marked(store: SituationStore) -> None:
    sid = _open_with_evidence(store)
    store.mark_weak([(sid, "a4")], weak_at=_NOW)

    got = store.unmarked_evidence_by_rule([sid], rules=("anchor", "nation", "token"))

    assert got == {sid: ["a2", "a3"]}


async def test_scan_uses_production_gate_and_keeps_unscorable(
    store: SituationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _open_with_evidence(store)

    async def fake_vectors(
        kept: Mapping[str, list[str]], titles: Mapping[str, str], repo: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return {sid: [1.0]}, {"a2": [1.0], "a3": [1.0]}  # a4 は埋込なし = 判定不能

    def fake_decider(
        kept: Mapping[str, list[str]], sit_vecs: Any, art_vecs: Any, **_: Any
    ) -> tuple[Callable[[str, str], tuple[bool, float | None]], str]:
        return (lambda s, a: (a == "a2", 0.5)), "ml"

    monkeypatch.setattr(stateful, "_gate_vectors", fake_vectors)
    monkeypatch.setattr(stateful, "_gate_decider", fake_decider)

    scan = await scan_weak_evidence(store._repo, store)  # noqa: SLF001

    assert scan.checked == 3
    assert scan.weak == ((sid, "a3"),)
    assert scan.judge == "ml"
