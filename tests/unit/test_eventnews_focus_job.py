"""焦点の記録 (F0): 毎時の段の後に焦点を計算して記録するだけ。判断には使わない。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.eventnews.models import MemberArticle
from src.storage.repo_merge_decisions import MergeDecisionRow
from src.storage.run_history import RunHistoryRepository
from src.ui.services.eventnews_focus_job import (
    enrich_decision_rows,
    focus_payload,
    focus_record_enabled,
    record_focuses,
    verify_recorded_focuses,
)
from src.ui.services.fill_rate_focus_section import (
    FocusAuditStats,
    collect_focus_audit_stats,
    focus_line,
)

_T0 = datetime(2026, 10, 24, 6, 0, 0, tzinfo=UTC)
CVE_A = ("cve", "CVE-2026-0001")
CVE_B = ("cve", "CVE-2026-0002")


def _member(aid: str, *names: tuple[str, str], kind: str = "", ts: datetime = _T0) -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=aid,
        url=f"https://x.example/{aid}",
        feed_title="F",
        feed_url="https://x.example/f",
        host="x.example",
        importance="medium",
        category="apt",
        status="posted",
        anchor_ts=ts,
        summary="s",
        body="b",
        entities=frozenset(names),
        kind=kind,
    )


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "focus.db")


def _item(
    repo: RunHistoryRepository, iid: str, articles: list[str], *, when: datetime = _T0
) -> None:
    repo.create_event_item(
        item_id=iid,
        origin="live",
        first_reported_at=when,
        last_reported_at=when,
        importance="medium",
        when=when,
    )
    for i, aid in enumerate(articles):
        repo.add_event_member(
            item_id=iid,
            article_id=aid,
            joined_at=when + timedelta(minutes=i),
            contributed_new_facts=0,
            join_signal="seed" if i == 0 else "cos",
        )


def _loader(
    members: dict[str, MemberArticle],
) -> Callable[[object, list[str], object], dict[str, MemberArticle]]:
    def load(_repo: object, ids: list[str], _counts: object) -> dict[str, MemberArticle]:
        return {a: members[a] for a in ids if a in members}

    return load


class TestRecordFocuses:
    def test_records_multi_member_item_and_skips_singletons(
        self, repo: RunHistoryRepository
    ) -> None:
        _item(repo, "ev-1", ["a1", "a2"])
        _item(repo, "ev-solo", ["s1"])
        members = {
            "a1": _member("a1", CVE_A),
            "a2": _member("a2", CVE_A),
            "s1": _member("s1", CVE_B),
        }

        stats = record_focuses(repo, now=_T0, load_members=_loader(members), counts={})

        assert stats.recorded == 1
        assert set(repo.get_item_focuses(["ev-1", "ev-solo"])) == {"ev-1"}

    def test_unchanged_item_is_not_recomputed_until_it_moves(
        self, repo: RunHistoryRepository
    ) -> None:
        _item(repo, "ev-1", ["a1", "a2"])
        members = {"a1": _member("a1", CVE_A), "a2": _member("a2", CVE_A)}
        record_focuses(repo, now=_T0 + timedelta(hours=1), load_members=_loader(members), counts={})

        again = record_focuses(
            repo, now=_T0 + timedelta(hours=2), load_members=_loader(members), counts={}
        )

        assert again.candidates == 0

    def test_item_update_triggers_rebuild_and_logs_the_name_change(
        self, repo: RunHistoryRepository
    ) -> None:
        _item(repo, "ev-1", ["a1", "a2"])
        members = {
            "a1": _member("a1", CVE_A),
            "a2": _member("a2", CVE_A),
            "a3": _member("a3", CVE_A, ("victim_org", "orgx")),
            "a4": _member("a4", CVE_A, ("victim_org", "orgx")),
        }
        record_focuses(repo, now=_T0 + timedelta(hours=1), load_members=_loader(members), counts={})
        for i, aid in enumerate(["a3", "a4"]):
            repo.add_event_member(
                item_id="ev-1",
                article_id=aid,
                joined_at=_T0 + timedelta(hours=2, minutes=i),
                contributed_new_facts=1,
                join_signal="cos",
            )
        repo.update_event_item("ev-1", {"updated_at": _T0 + timedelta(hours=3)})

        stats = record_focuses(
            repo, now=_T0 + timedelta(hours=4), load_members=_loader(members), counts={}
        )

        assert (stats.recorded, stats.changed) == (1, 1)
        with repo._connect() as conn:
            rows = conn.execute(
                "SELECT cause, added_json FROM event_item_focus_log ORDER BY id"
            ).fetchall()
        assert [r["cause"] for r in rows] == ["seed", "join"]
        assert "orgx" in rows[1]["added_json"]

    def test_absorbed_item_focus_is_dropped(self, repo: RunHistoryRepository) -> None:
        _item(repo, "ev-1", ["a1", "a2"])
        members = {"a1": _member("a1", CVE_A), "a2": _member("a2", CVE_A)}
        record_focuses(repo, now=_T0 + timedelta(hours=1), load_members=_loader(members), counts={})
        repo.update_event_item(
            "ev-1", {"merged_into": "ev-0", "updated_at": _T0 + timedelta(hours=2)}
        )

        stats = record_focuses(
            repo, now=_T0 + timedelta(hours=3), load_members=_loader(members), counts={}
        )

        assert stats.dropped_merged == 1
        assert repo.get_item_focuses(["ev-1"]) == {}

    def test_merge_cause_links_the_decision(self, repo: RunHistoryRepository) -> None:
        _item(repo, "ev-1", ["a1", "a2"])
        members = {
            "a1": _member("a1", CVE_A),
            "a2": _member("a2", CVE_A),
            "b1": _member("b1", CVE_A, ("victim_org", "orgx")),
            "b2": _member("b2", CVE_A, ("victim_org", "orgx")),
        }
        record_focuses(repo, now=_T0 + timedelta(hours=1), load_members=_loader(members), counts={})
        repo.record_merge_decisions(
            [
                MergeDecisionRow(
                    target_id="ev-1",
                    absorbed_id="ev-2",
                    run_kind="hourly",
                    edges_json="[]",
                    edge_count=0,
                    rescued=False,
                    flags_json="{}",
                )
            ],
            decided_at=_T0 + timedelta(hours=2),
        )
        for aid in ("b1", "b2"):
            repo.add_event_member(
                item_id="ev-1",
                article_id=aid,
                joined_at=_T0 + timedelta(hours=2),
                contributed_new_facts=0,
                join_signal="retro_merge",
            )
        repo.update_event_item("ev-1", {"updated_at": _T0 + timedelta(hours=2)})

        record_focuses(repo, now=_T0 + timedelta(hours=3), load_members=_loader(members), counts={})

        with repo._connect() as conn:
            row = conn.execute(
                "SELECT cause, decision_id FROM event_item_focus_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row["cause"] == "merge"
        assert row["decision_id"] is not None

    def test_flag_off_records_nothing(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVENTNEWS_FOCUS_RECORD", "0")
        _item(repo, "ev-1", ["a1", "a2"])
        members = {"a1": _member("a1", CVE_A), "a2": _member("a2", CVE_A)}

        stats = record_focuses(repo, now=_T0, load_members=_loader(members), counts={})

        assert stats.skipped == "flag_off"
        assert not focus_record_enabled()
        assert repo.get_item_focuses(["ev-1"]) == {}

    def test_replay_matches_db_value(self, repo: RunHistoryRepository) -> None:
        _item(repo, "ev-1", ["a1", "a2", "a3"])
        members = {
            "a1": _member("a1", CVE_A),
            "a2": _member("a2", CVE_A),
            "a3": _member("a3", CVE_B),
        }
        record_focuses(repo, now=_T0, load_members=_loader(members), counts={})

        bad = verify_recorded_focuses(repo, ["ev-1"], load_members=_loader(members), counts={})

        assert bad == []


class TestEnrichDecisionRows:
    def _row(self) -> MergeDecisionRow:
        return MergeDecisionRow(
            target_id="t",
            absorbed_id="x",
            run_kind="hourly",
            edges_json="[]",
            edge_count=0,
            rescued=False,
            flags_json="{}",
        )

    def test_conflicting_cves_are_recorded_as_conflict(self) -> None:
        members = {"t": [_member("a", CVE_A)], "x": [_member("b", CVE_B)]}

        (row,) = enrich_decision_rows([self._row()], lambda i: members[i])

        assert row.focus_class == "conflict"
        assert '"target"' in row.focus_json and '"absorbed"' in row.focus_json

    def test_failure_keeps_original_row(self) -> None:
        def boom(_: str) -> list[MemberArticle]:
            raise RuntimeError("x")

        (row,) = enrich_decision_rows([self._row()], boom)

        assert row.focus_class == ""

    def test_flag_off_leaves_rows_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVENTNEWS_FOCUS_RECORD", "0")

        (row,) = enrich_decision_rows([self._row()], lambda _: [_member("a", CVE_A)])

        assert row.focus_class == ""

    def test_decision_columns_roundtrip_through_repo(self, repo: RunHistoryRepository) -> None:
        members = {"t": [_member("a", CVE_A)], "x": [_member("b", CVE_B)]}
        (row,) = enrich_decision_rows([self._row()], lambda i: members[i])

        repo.record_merge_decisions([row], decided_at=_T0)

        (got,) = repo.list_merge_decisions()
        assert got.focus_class == "conflict"
        assert got.focus_json == row.focus_json


class TestPayloadAndAudit:
    def test_payload_has_names_kind_label_and_counts(self, repo: RunHistoryRepository) -> None:
        _item(repo, "ev-1", ["a1", "a2"])
        members = {
            "a1": _member("a1", CVE_A, kind="advisory"),
            "a2": _member("a2", CVE_A, kind="advisory"),
        }
        record_focuses(repo, now=_T0, load_members=_loader(members), counts={})

        payload = focus_payload(repo, "ev-1")

        assert payload is not None
        assert payload["kind"] == "advisory"
        assert payload["kind_label"]
        assert payload["supporters"] == 2

    def test_payload_none_when_not_recorded(self, repo: RunHistoryRepository) -> None:
        assert focus_payload(repo, "nope") is None

    def test_audit_counts_non_empty_and_heavy_outsiders(self, repo: RunHistoryRepository) -> None:
        _item(repo, "ev-1", ["a1", "a2"])
        _item(repo, "ev-2", ["b1", "b2", "b3"])
        members = {
            "a1": _member("a1", CVE_A),
            "a2": _member("a2", CVE_A),
            "b1": _member("b1", CVE_A),
            "b2": _member("b2", CVE_B),
            "b3": _member("b3", CVE_B),
        }
        record_focuses(repo, now=_T0, load_members=_loader(members), counts={})

        stats = collect_focus_audit_stats(repo)

        assert (stats.multi_items, stats.recorded, stats.non_empty) == (2, 2, 2)
        assert stats.heavy_outsiders == 1  # b 側は外れ 2/3

    def test_line_warns_when_many_are_empty(self) -> None:
        line, warn = focus_line(
            FocusAuditStats(multi_items=100, recorded=100, non_empty=50, heavy_outsiders=3)
        )

        assert warn is True
        assert "50%" in line

    def test_line_does_not_warn_on_few_items(self) -> None:
        _, warn = focus_line(
            FocusAuditStats(multi_items=5, recorded=5, non_empty=0, heavy_outsiders=0)
        )

        assert warn is False
