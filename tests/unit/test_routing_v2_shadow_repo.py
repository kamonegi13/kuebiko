"""routing_v2_shadow テーブルの読み書き (2026-10-10、M3b 段1)。"""

from __future__ import annotations

from pathlib import Path

from src.storage.repo_routing_v2_shadow import RoutingV2ShadowRow
from src.storage.run_history import RunHistoryRepository


def _row(i: int, *, old: str = "watch", new: str = "watch", **kw: object) -> RoutingV2ShadowRow:
    base = {
        "article_id": f"rss:{i}",
        "old_channel": old,
        "old_rule_id": "R7.fallback",
        "new_channel": new,
        "new_rule_id": "V2.fallback",
        "severity": "",
        "severity_basis": "",
        "jp": "",
        "relevant": False,
        "importance": "low",
    }
    base.update(kw)
    return RoutingV2ShadowRow(**base)  # type: ignore[arg-type]


def test_record_and_summarize_crosstab(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_routing_v2_shadow(_row(1, old="watch", new="watch"))
    repo.record_routing_v2_shadow(_row(2, old="watch", new="vuln"))
    repo.record_routing_v2_shadow(_row(3, old="alert", new="alert"))

    crosstab = {
        (c.old_channel, c.new_channel): c.count for c in repo.summarize_routing_v2_shadow(days=7)
    }

    assert crosstab[("watch", "watch")] == 1
    assert crosstab[("watch", "vuln")] == 1
    assert crosstab[("alert", "alert")] == 1


def test_list_changed_only_returns_mismatches(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_routing_v2_shadow(_row(1, old="watch", new="watch"))
    repo.record_routing_v2_shadow(_row(2, old="watch", new="vuln"))
    repo.record_routing_v2_shadow(_row(3, old="alert", new="japan_watch"))

    changed = repo.list_routing_v2_shadow_changed(days=7)

    assert {r.article_id for r in changed} == {"rss:2", "rss:3"}
    assert all(r.changed for r in changed)


def test_fields_round_trip(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_routing_v2_shadow(
        _row(
            1,
            old="watch",
            new="vuln",
            severity="S3",
            severity_basis="kev",
            jp="none",
            relevant=False,
            importance="high",
        )
    )

    changed = repo.list_routing_v2_shadow_changed(days=7)

    assert len(changed) == 1
    got = changed[0]
    assert got.severity == "S3"
    assert got.severity_basis == "kev"
    assert got.jp == "none"
    assert got.relevant is False
    assert got.importance == "high"


def test_purge_removes_old_rows(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_routing_v2_shadow(_row(1))

    purged = repo.purge_routing_v2_shadow(days=0)

    assert purged == 1
    assert repo.summarize_routing_v2_shadow(days=7) == []
