"""判定ごとの属性の読み込み (パターンまとめの材料、SQLite)。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.storage.repo_judgment_attrs import JudgmentAttrs, since_iso
from src.storage.run_history import RunHistoryRepository, RunRecord

SINCE = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
UNTIL = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)
NEW = "2026-11-17T03:00:00+00:00"
OLD = "2026-11-10T03:00:00+00:00"


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    r = RunHistoryRepository(db_path=tmp_path / "attrs.db")
    r.start_run(RunRecord(started_at=SINCE, pipeline="p", dry_run=False, triggered_by="manual"))
    return r


def _evidence(
    conn: Any, sid: str, aid: str, *, added: str = NEW, assessed: bool = True, weak: bool = False
) -> None:
    conn.execute(
        "INSERT INTO situation_evidence (situation_id, article_id, added_at, assessed_at, weak_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (sid, aid, added, added if assessed else None, added if weak else None),
    )


def _article(conn: Any, aid: str, country: str | None, kind: str | None) -> None:
    conn.execute(
        "INSERT INTO articles"
        " (run_id, article_id, title, url, status, victim_country_iso, created_at)"
        " VALUES (1, ?, 't', 'https://kuebiko.example/x', 'posted', ?, ?)",
        (aid, country, NEW),
    )
    if kind:
        conn.execute(
            "INSERT INTO article_kinds (article_id, kind, model, created_at) VALUES (?, ?, 'm', ?)",
            (aid, kind, NEW),
        )


def _event(conn: Any, item: str, aid: str, *, merged_into: str | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO event_items (id, origin, first_reported_at, last_reported_at,"
        " merged_into, created_at, updated_at) VALUES (?, 'live', ?, ?, ?, ?, ?)",
        (item, NEW, NEW, merged_into, NEW, NEW),
    )
    conn.execute(
        "INSERT INTO event_item_members (item_id, article_id, joined_at) VALUES (?, ?, ?)",
        (item, aid, NEW),
    )


def test_since_iso_is_utc_microseconds() -> None:
    assert since_iso(SINCE) == "2026-11-17T00:00:00.000000+00:00"


def test_collects_attributes_from_assessed_recent_evidence(repo: RunHistoryRepository) -> None:
    with repo._connect() as conn:
        _article(conn, "a1", "JP", "breach")
        _evidence(conn, "s1", "a1")
        conn.execute(
            "INSERT INTO article_importance_v2 (article_id, severity, severity_basis, jp, nations,"
            " sir_ids, relevant, rule_version, created_at)"
            " VALUES ('a1', 'S3', 'b', 'j', 'n', 's', 1, 'v', ?)",
            (NEW,),
        )
        _event(conn, "ev-child", "a1", merged_into="ev-root")
    got = repo.get_judgment_attrs(["s1", "s-none"], since=SINCE, until=UNTIL)
    assert got == {
        "s1": JudgmentAttrs(
            countries=frozenset({"JP"}),
            kinds=frozenset({"breach"}),
            severities=frozenset({"S3"}),
            event_roots=frozenset({"ev-root"}),
        )
    }


def test_excludes_old_unassessed_weak_and_roundup_evidence(repo: RunHistoryRepository) -> None:
    with repo._connect() as conn:
        for aid, added, assessed, weak in (
            ("old", OLD, True, False),
            ("unassessed", NEW, False, False),
            ("weak", NEW, True, True),
            ("roundup", NEW, True, False),
        ):
            _article(conn, aid, "US", "roundup" if aid == "roundup" else "breach")
            _evidence(conn, "s1", aid, added=added, assessed=assessed, weak=weak)
        _article(conn, "ok", "JP", "exploitation")
        _evidence(conn, "s1", "ok")
    got = repo.get_judgment_attrs(["s1"], since=SINCE, until=UNTIL)
    assert got["s1"].countries == frozenset({"JP"})
    assert got["s1"].kinds == frozenset({"exploitation"})


def test_empty_ids_returns_empty(repo: RunHistoryRepository) -> None:
    assert repo.get_judgment_attrs([], since=SINCE, until=UNTIL) == {}


def test_evidence_added_after_until_is_excluded(repo: RunHistoryRepository) -> None:
    with repo._connect() as conn:
        _article(conn, "late", "US", "breach")
        _evidence(conn, "s1", "late", added="2026-11-18T03:00:00+00:00")
    assert repo.get_judgment_attrs(["s1"], since=SINCE, until=UNTIL) == {}


def test_evidence_added_at_until_itself_is_included(repo: RunHistoryRepository) -> None:
    until = datetime(2026, 11, 17, 10, 30, 2, 538567, tzinfo=UTC)
    with repo._connect() as conn:
        _article(conn, "at-end", "JP", "breach")
        _evidence(conn, "s1", "at-end", added="2026-11-17T10:30:02.538567+00:00")
    got = repo.get_judgment_attrs(["s1"], since=SINCE, until=until)
    assert got["s1"].countries == frozenset({"JP"})


def _version(conn: Any, item: str, version: int, headline: str, at: str) -> None:
    conn.execute(
        "INSERT INTO event_item_versions (item_id, version, generated_at, model, prompt_version,"
        " headline, body_json, new_facts_json) VALUES (?, ?, ?, 'm', 'p', ?, '{}', '[]')",
        (item, version, at, headline),
    )


def test_loads_event_headline_entities_magnitude_and_last_evidence(
    repo: RunHistoryRepository,
) -> None:
    with repo._connect() as conn:
        for aid in ("a1", "a2"):
            _article(conn, aid, "JP", "breach")
            _evidence(conn, "s1", aid)
            _event(conn, "ev-1", aid)
        conn.execute(
            "INSERT INTO article_importance_v2 (article_id, severity, severity_basis, jp, nations,"
            " sir_ids, relevant, rule_version, created_at)"
            " VALUES ('a1', 'S3', 'large_magnitude', 'j', 'n', 's', 1, 'v', ?)",
            (NEW,),
        )
        for aid, etype, value in (
            ("a1", "ttp", "T1566"),
            ("a2", "affected_product", "VPN-X"),
            ("a2", "cve", "CVE-2026-0001"),
        ):
            conn.execute(
                "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
                " VALUES (?, ?, ?, ?)",
                (aid, etype, value, NEW),
            )
        _version(conn, "ev-1", 1, "旧い見出し", OLD)
        _version(conn, "ev-1", 2, "最新の見出し", NEW)
        conn.execute(
            "INSERT INTO situations (situation_id, title, domain, status, anchors, pir_ids,"
            " opened_at, last_evidence_at) VALUES ('s1', 't', 'd', 'open', '[]', '[]', ?, ?)",
            (NEW, NEW),
        )
    got = repo.get_judgment_attrs(["s1"], since=SINCE, until=UNTIL)["s1"]
    assert got.large_magnitude
    assert got.links == frozenset({"cve:CVE-2026-0001", "affected_product:VPN-X"})
    assert (got.techniques, got.products) == (frozenset({"T1566"}), frozenset({"VPN-X"}))
    assert [(h.root, h.articles, h.headline) for h in got.event_headlines] == [
        ("ev-1", 2, "最新の見出し")
    ]
    assert got.last_evidence_at == NEW


def test_situation_without_period_evidence_still_returns_last_evidence(
    repo: RunHistoryRepository,
) -> None:
    with repo._connect() as conn:
        conn.execute(
            "INSERT INTO situations (situation_id, title, domain, status, anchors, pir_ids,"
            " opened_at, last_evidence_at) VALUES ('s2', 't', 'd', 'open', '[]', '[]', ?, ?)",
            (OLD, OLD),
        )
    got = repo.get_judgment_attrs(["s2"], since=SINCE, until=UNTIL)
    assert got["s2"].last_evidence_at == OLD and got["s2"].event_headlines == ()
