"""GRAPH-G0: 導いた線の日次の写し (event_edge_records)・tier の定数・日次の段。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.eventnews.relations import DerivedRelation
from src.graph import edge_tiers
from src.graph.edge_records import (
    records_from_lineage,
    records_from_relations,
)
from src.storage.run_history import RunHistoryRepository
from src.ui.services import graph_edges_daily

DAY = "2026-11-02"


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "e.db")


def _rels() -> list[DerivedRelation]:
    return [
        DerivedRelation("e1", "e2", "follow_up", ("victim:x",), {"p": "0.91", "cos": "0.700"}),
        DerivedRelation("e1", "e2", "follow_up", ("victim:x",), {"p": "0.91", "cos": "0.700"}),
        DerivedRelation("e1", "e3", "same_actor", ("actor:g",), {"p": "0.10", "cos": "0.640"}),
    ]


def test_all_edge_types_start_at_check() -> None:
    assert set(edge_tiers.EDGE_TIERS) == set(edge_tiers.EDGE_TYPES)
    assert set(edge_tiers.EDGE_TIERS.values()) == {edge_tiers.TIER_CHECK}


def test_tier_maps_rel_types_and_unknown_falls_to_check() -> None:
    assert edge_tiers.edge_type_of("follow_up") == edge_tiers.EDGE_INCIDENT
    assert edge_tiers.edge_type_of("same_actor") == edge_tiers.EDGE_SAME_ACTOR
    assert edge_tiers.tier_for_rel_type("side") == edge_tiers.TIER_CHECK
    assert edge_tiers.tier_for_rel_type("never_seen") == edge_tiers.TIER_CHECK


def test_records_dedupe_and_map_scores() -> None:
    rows = records_from_relations(_rels(), derived_at=DAY, model_sha="abc")
    assert len(rows) == 2
    inc = next(r for r in rows if r.edge_type == "incident")
    act = next(r for r in rows if r.edge_type == "same_actor")
    assert inc.score == 0.91 and inc.rel_type == "follow_up" and inc.tier == "check"
    assert act.score == 0.64
    assert inc.model_sha == "abc" and inc.source == "derived"
    assert inc.edge_key.startswith("e1|e2|incident|")


def test_lineage_records_use_roots_and_sorted_ids() -> None:
    rows = records_from_lineage(
        [("b", "a", "veto"), ("a", "b", "decision")],
        derived_at=DAY,
        roots={"a": "root-a"},
        rule_version="f1b-v1",
    )
    assert len(rows) == 1
    r = rows[0]
    assert (r.item_a, r.item_b, r.root_a, r.root_b) == ("a", "b", "root-a", "b")
    assert r.edge_type == "lineage" and r.score is None


def test_replace_is_idempotent_for_same_day(repo: RunHistoryRepository) -> None:
    rows = records_from_relations(_rels(), derived_at=DAY, model_sha="")
    repo.replace_event_edge_records(DAY, rows)
    repo.replace_event_edge_records(DAY, rows[:1])
    other = records_from_relations(_rels(), derived_at="2026-11-01", model_sha="")
    repo.replace_event_edge_records("2026-11-01", other)
    assert sum(repo.edge_record_stats(DAY).values()) == 1
    assert sum(repo.edge_record_stats("2026-11-01").values()) == 2


def test_same_pair_with_multiple_rel_types_does_not_violate_unique(
    repo: RunHistoryRepository,
) -> None:
    rels = [
        DerivedRelation("e1", "e2", "follow_up", ("victim:x",), {"p": "0.9"}),
        DerivedRelation("e2", "e1", "side", ("victim:x",), {"p": "0.8"}),
        DerivedRelation("e1", "e2", "contains", ("victim:x",), {"p": "0.7"}),
    ]
    rows = records_from_relations(rels, derived_at=DAY, model_sha="")
    assert repo.replace_event_edge_records(DAY, rows) == 1
    assert repo.edge_record_stats(DAY) == {"incident": 1}


def test_replace_rolls_back_and_keeps_previous_rows_on_error(
    repo: RunHistoryRepository,
) -> None:
    good = records_from_relations(_rels(), derived_at=DAY, model_sha="")
    repo.replace_event_edge_records(DAY, good)
    # 同じ edge_key を 2 行にして UNIQUE 違反を INSERT の途中で起こす
    dup = [good[0], good[0]]
    with pytest.raises(Exception):  # noqa: B017, PT011 (sqlite3 / psycopg の違反)
        repo.replace_event_edge_records(DAY, dup)
    assert sum(repo.edge_record_stats(DAY).values()) == len(good)


def test_purge_drops_only_old_days(repo: RunHistoryRepository) -> None:
    old = (datetime.now(UTC) - timedelta(days=200)).date().isoformat()
    new = datetime.now(UTC).date().isoformat()
    repo.replace_event_edge_records(
        old, records_from_relations(_rels(), derived_at=old, model_sha="")
    )
    repo.replace_event_edge_records(
        new, records_from_relations(_rels(), derived_at=new, model_sha="")
    )
    assert repo.purge_event_edge_records(180) == 2
    assert repo.edge_record_stats(old) == {}
    assert sum(repo.edge_record_stats(new).values()) == 2


def test_event_roots_follow_merged_into(repo: RunHistoryRepository) -> None:
    with repo._connect() as conn:  # noqa: SLF001
        now = datetime.now(UTC).isoformat()
        for iid, into in (("x", "y"), ("y", ""), ("z", "")):
            conn.execute(
                "INSERT INTO event_items (id, origin, first_reported_at, last_reported_at,"
                " merged_into, created_at, updated_at) VALUES (?, 'live', ?, ?, ?, ?, ?)",
                (iid, now, now, into or None, now, now),
            )
    assert repo.event_roots(["x", "y", "z"]) == {"x": "y", "y": "y", "z": "z"}


def test_daily_job_is_noop_when_flag_off(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
) -> None:
    monkeypatch.delenv(graph_edges_daily.FLAG, raising=False)
    assert asyncio.run(graph_edges_daily.run_graph_edges_daily(repo)) == {"skipped": "flag_off"}
    assert repo.edge_record_stats(datetime.now(UTC).date().isoformat()) == {}


def test_daily_job_writes_and_rerun_replaces(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
) -> None:
    monkeypatch.setenv(graph_edges_daily.FLAG, "1")
    index: dict[str, list[DerivedRelation]] = {"e1": _rels()[:1], "e2": _rels()[:1]}
    monkeypatch.setattr(graph_edges_daily, "relations_by_event", lambda r: index)
    first = asyncio.run(graph_edges_daily.run_graph_edges_daily(repo))
    second = asyncio.run(graph_edges_daily.run_graph_edges_daily(repo))
    assert first["written"] == second["written"] == 1
    assert repo.edge_record_stats(str(first["derived_at"])) == {"incident": 1}


def test_lineage_pairs_come_from_vetoes_and_decisions(repo: RunHistoryRepository) -> None:
    from src.storage.repo_merge_decisions import MergeDecisionRow

    now = datetime.now(UTC)
    repo.record_merge_decisions(
        [
            MergeDecisionRow("t", "ab", "hourly", "[]", 1, False, "{}", focus_class="lineage"),
            MergeDecisionRow("t", "cd", "hourly", "[]", 1, False, "{}", focus_class="support"),
        ],
        decided_at=now,
    )
    pairs: list[tuple[str, str, str]] = graph_edges_daily.collect_lineage_pairs(repo, now=now)
    assert pairs == [("t", "ab", "decision")]


def _call_api(item_id: str, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from src.ui.api import eventnews as api

    class _Repo:
        def latest_event_versions(self, ids: list[str]) -> dict[str, Any]:
            return {}

        def list_event_members(self, item_id: str) -> list[Any]:
            return []

        def get_articles_by_ids(self, ids: list[str]) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(api, "_repo", lambda: _Repo())
    index = {"e1": _rels()}
    monkeypatch.setattr("src.eventnews.relations.relations_by_event", lambda r: index)
    return list(api.event_news_relations(item_id)["relations"])


def test_api_relations_carry_tier_from_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    got = _call_api("e1", monkeypatch)
    assert got and all(g["tier"] == "check" for g in got)
    monkeypatch.setitem(edge_tiers.EDGE_TIERS, "same_actor", edge_tiers.TIER_WRITER)
    tiers = {g["rel_type"]: g["tier"] for g in _call_api("e1", monkeypatch)}
    assert tiers["same_actor"] == "writer" and tiers["follow_up"] == "check"


def test_fill_rate_metrics_registered_and_run_on_sqlite(repo: RunHistoryRepository) -> None:
    from src.storage.db_backend import connect
    from src.ui.services.fill_rate_audit import METRICS, collect_weekly_cells

    keys = {m.key for m in METRICS}
    assert {"event_edge_records", "event_edge_coverage"} <= keys
    con = connect(repo.db_path)
    try:
        cells = collect_weekly_cells(con, now=datetime.now(UTC))
    finally:
        con.close()
    assert {"event_edge_records", "event_edge_coverage"} <= set(cells)
