"""勧告のハブへの吸収の関門 (EVENTNEWS_MERGE_HUB_SPLIT、2026-11-11)。

- ハブ = 統合の前の記事数 8 以上 + seed の種別 advisory。
- 相手の seed が advisory 以外 → 止める / advisory でも辺 1 本の救済 → 止める / 未分類は止めない。
- shadow は判断記録に印だけ付けて結ぶ。1 は結ばず、焦点の照合も当てない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from src.eventnews.hub_split import (
    HUB_SPLIT_CLASS,
    HUB_SPLIT_MIN_ARTICLES,
    ItemProfile,
    build_hub_split_check,
    hub_split_reason,
    profile_of,
    seed_kind,
)
from src.eventnews.merge import (
    HUB_MIN_ARTICLES,
    LinkVerdict,
    MergeGroup,
    MergeLink,
    VetoedLink,
    plan_merges,
)
from src.eventnews.models import MemberArticle
from src.storage.repo_merge_decisions import (
    ACTION_HUB_SPLIT_BLOCKED,
    MergeDecisionRow,
)
from src.storage.run_history import RunHistoryRepository
from src.ui.services import eventnews_merge_job as job
from src.ui.services import graph_edges_daily
from src.ui.services.eventnews_merge_decisions import HUB_SPLIT_FLAG_KEY, mark_hub_split

_T0 = datetime(2026, 11, 11, 6, 0, 0, tzinfo=UTC)
_CVE = ("cve", "cve-2026-1")
_ACTOR = ("actor", "lazarus")

HUB = ItemProfile(article_count=HUB_SPLIT_MIN_ARTICLES, seed_kind="advisory")


def _member(aid: str, kind: str = "", *names: tuple[str, str]) -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=aid,
        url=f"https://x.example/{aid}",
        feed_title="F",
        feed_url="https://x.example/f",
        host="x.example",
        importance="medium",
        category="vuln",
        status="posted",
        anchor_ts=_T0,
        summary="s",
        body="b",
        entities=frozenset(names or (_CVE,)),
        kind=kind,
    )


class TestHubSplitReason:
    def test_exploitation_into_an_advisory_hub_is_stopped(self) -> None:
        other = ItemProfile(1, "exploitation")

        assert hub_split_reason(HUB, other, rescued=False) == "kind:exploitation"

    @pytest.mark.parametrize("kind", ["breach", "stats", "other", "derivative"])
    def test_any_non_advisory_seed_is_stopped(self, kind: str) -> None:
        assert hub_split_reason(HUB, ItemProfile(1, kind), rescued=False) == f"kind:{kind}"

    def test_advisory_with_enough_edges_passes(self) -> None:
        assert hub_split_reason(HUB, ItemProfile(1, "advisory"), rescued=False) == ""

    def test_advisory_rescued_by_a_single_edge_is_stopped(self) -> None:
        assert hub_split_reason(HUB, ItemProfile(1, "advisory"), rescued=True) == "rescued"

    def test_unclassified_partner_is_not_stopped(self) -> None:
        assert hub_split_reason(HUB, ItemProfile(1, ""), rescued=True) == ""

    def test_small_advisory_event_is_not_a_hub(self) -> None:
        small = ItemProfile(HUB_SPLIT_MIN_ARTICLES - 1, "advisory")

        assert hub_split_reason(small, ItemProfile(1, "exploitation"), rescued=False) == ""

    def test_large_exploitation_event_is_not_a_hub(self) -> None:
        big = ItemProfile(50, "exploitation")

        assert hub_split_reason(big, ItemProfile(1, "advisory"), rescued=True) == ""

    def test_hub_on_either_side(self) -> None:
        assert hub_split_reason(ItemProfile(1, "exploitation"), HUB, rescued=False) != ""

    def test_threshold_matches_the_merge_hub_definition(self) -> None:
        assert HUB_SPLIT_MIN_ARTICLES == HUB_MIN_ARTICLES


class TestSeedKind:
    def test_seed_is_the_focus_anchor(self) -> None:
        members = [_member("a1", "advisory"), _member("a2", "exploitation")]

        assert seed_kind(members) == "advisory"

    def test_roundup_is_skipped(self) -> None:
        members = [_member("r1", "roundup"), _member("a1", "exploitation")]

        assert seed_kind(members) == "exploitation"

    def test_majority_drift_does_not_change_the_seed(self) -> None:
        # 混ざった後に悪用の記事が多数になっても、ハブの種別は seed のまま
        members = [_member("a1", "advisory")] + [_member(f"x{i}", "exploitation") for i in range(9)]

        assert profile_of(members) == ItemProfile(10, "advisory")

    def test_unclassified_seed_is_empty(self) -> None:
        assert seed_kind([_member("a1", "")]) == ""

    def test_derivative_alias_is_normalized(self) -> None:
        assert seed_kind([_member("a1", "enforcement")]) == "derivative"

    def test_no_members(self) -> None:
        assert seed_kind([]) == ""


class TestBuildHubSplitCheck:
    def test_uses_members_once_per_item(self) -> None:
        calls: list[str] = []
        members = {
            "hub": [_member(f"h{i}", "advisory") for i in range(HUB_SPLIT_MIN_ARTICLES)],
            "x": [_member("x1", "exploitation")],
        }

        def members_of(i: str) -> list[MemberArticle]:
            calls.append(i)
            return members[i]

        check = build_hub_split_check(members_of)

        assert check("hub", "x", False) == "kind:exploitation"
        assert check("x", "hub", False) == "kind:exploitation"
        assert sorted(calls) == ["hub", "x"]

    def test_failure_does_not_block(self) -> None:
        def boom(_: str) -> list[MemberArticle]:
            raise RuntimeError("db down")

        assert build_hub_split_check(boom)("a", "b", True) == ""


def _v(x: float, y: float) -> np.ndarray:
    v = np.array([x, y], dtype=float)
    out: np.ndarray = v / np.linalg.norm(v)
    return out


def _plan(
    reason: str,
    *,
    enforce: bool,
    out: list[VetoedLink] | None = None,
    asked_focus: list[tuple[str, str]] | None = None,
) -> list[MergeGroup]:
    ents = {k: frozenset({_ACTOR, _CVE}) for k in ("a1", "a2", "b1", "b2")}
    vecs = {"a1": _v(1, 0), "a2": _v(1, 0.01), "b1": _v(1, 0.02), "b2": _v(1, 0.03)}

    def focus(a: str, b: str) -> LinkVerdict | None:
        if asked_focus is not None:
            asked_focus.append((a, b))
        return None

    return plan_merges(
        item_of={"a1": "i1", "a2": "i1", "b1": "i2", "b2": "i2"},
        first_seen={"i1": "2026-08-01", "i2": "2026-08-10"},
        entities=ents,
        vectors=vecs,
        link_veto=focus,
        hub_split=lambda a, b, rescued: reason,
        hub_split_enforce=enforce,
        hub_split_out=out,
    )


class TestPlanMergesHubSplit:
    def test_shadow_records_and_still_merges(self) -> None:
        out: list[VetoedLink] = []

        got = _plan("kind:exploitation", enforce=False, out=out)

        assert len(got) == 1
        assert [(v.klass, v.detail, v.enforced) for v in out] == [
            (HUB_SPLIT_CLASS, "kind:exploitation", False)
        ]
        assert out[0].edge_count == 4

    def test_enforce_blocks_and_skips_the_focus_check(self) -> None:
        out: list[VetoedLink] = []
        asked: list[tuple[str, str]] = []

        got = _plan("rescued", enforce=True, out=out, asked_focus=asked)

        assert got == []
        assert out[0].enforced is True
        assert asked == []

    def test_empty_reason_passes_without_a_record(self) -> None:
        out: list[VetoedLink] = []

        assert len(_plan("", enforce=True, out=out)) == 1
        assert out == []

    def test_rescued_flag_is_passed_to_the_check(self) -> None:
        seen: list[bool] = []

        def check(a: str, b: str, rescued: bool) -> str:
            seen.append(rescued)
            return ""

        plan_merges(
            item_of={"a1": "i1", "a2": "i1", "b1": "i2"},
            first_seen={"i1": "1", "i2": "2"},
            entities={k: frozenset({_ACTOR, _CVE}) for k in ("a1", "a2", "b1")},
            vectors={"a1": _v(1, 0), "a2": _v(1, 0.01), "b1": _v(1, 0.02)},
            single_edge_ok={("a1", "b1"), ("a2", "b1")},
            hub_split=check,
        )

        # 単独事象 i2 は辺 2 本 (a1-b1, a2-b1) なので救済ではない
        assert seen == [False]


def _row(absorbed: str) -> MergeDecisionRow:
    return MergeDecisionRow(
        "t", absorbed, "hourly", "[]", 2, False, json.dumps({"focus_gate": "on"})
    )


class TestMarkHubSplit:
    def test_marks_only_rows_touching_a_flagged_link(self) -> None:
        flagged = [VetoedLink("t", "x", HUB_SPLIT_CLASS, "kind:exploitation", 3, False)]

        got = mark_hub_split([_row("x"), _row("y")], flagged)

        assert json.loads(got[0].flags_json) == {
            "focus_gate": "on",
            HUB_SPLIT_FLAG_KEY: "kind:exploitation",
        }
        assert json.loads(got[1].flags_json) == {"focus_gate": "on"}

    def test_no_flags_returns_rows_unchanged(self) -> None:
        rows = [_row("x")]

        assert mark_hub_split(rows, []) == rows


class TestHubSplitMode:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, "shadow"),
            ("shadow", "shadow"),
            ("junk", "shadow"),
            ("1", "1"),
            ("on", "1"),
            ("0", "0"),
            ("off", "0"),
        ],
    )
    def test_parse(self, monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: str) -> None:
        if raw is None:
            monkeypatch.delenv(job.HUB_SPLIT_FLAG, raising=False)
        else:
            monkeypatch.setenv(job.HUB_SPLIT_FLAG, raw)

        assert job.hub_split_mode() == expected

    def test_flag_snapshot_records_the_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(job.HUB_SPLIT_FLAG, "1")

        assert job.merge_flag_snapshot()["hub_split"] == "1"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunHistoryRepository:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return RunHistoryRepository(db_path=tmp_path / "rh.db")


def _plan_with(splits: tuple[VetoedLink, ...]) -> job.MergePlan:
    return job.MergePlan(hub_splits=splits)


class TestRecordHubSplitBlocks:
    def test_only_enforced_blocks_are_written_with_their_own_action(
        self, repo: RunHistoryRepository
    ) -> None:
        splits = (
            VetoedLink("hub", "x", HUB_SPLIT_CLASS, "kind:exploitation", 3, True),
            VetoedLink("hub", "y", HUB_SPLIT_CLASS, "rescued", 1, False),
        )

        n = job.record_vetoes(repo, _plan_with(splits), _T0)

        rows = repo.list_merge_vetoes(since=datetime(2026, 1, 1, tzinfo=UTC))
        assert n == 1
        assert [(r.item_a, r.item_b, r.klass, r.action) for r in rows] == [
            ("hub", "x", HUB_SPLIT_CLASS, ACTION_HUB_SPLIT_BLOCKED)
        ]

    def test_blocked_pairs_are_lineage_candidates_for_the_graph(
        self, repo: RunHistoryRepository
    ) -> None:
        splits = (VetoedLink("hub", "x", HUB_SPLIT_CLASS, "kind:exploitation", 3, True),)
        job.record_vetoes(repo, _plan_with(splits), _T0)

        pairs = graph_edges_daily.collect_lineage_pairs(repo, now=_T0)

        assert ("hub", "x", "veto") in pairs


def test_merge_link_is_unchanged_by_the_shadow() -> None:
    # shadow の計画は旗なしの計画と同じ群を返す (結ぶ・結ばないを変えない)
    shadow = _plan("kind:exploitation", enforce=False)
    plain = _plan("", enforce=False)

    assert [g.absorbed for g in shadow] == [g.absorbed for g in plain]
    assert all(isinstance(lk, MergeLink) for g in shadow for lk in g.links)
