"""F2: 統合の判断で、焦点の照合が conflict の組を結ばない (2026-10-24)。

- conflict は結ばない / lineage は止めず関係の候補として記録だけ / support・weak は素通し。
- shadow は記録するが結ぶ。off は照合を呼ばない。
- 照合は「結ぶ条件を満たした事象の組」にだけ当てる (辺の足りない組は元々結ばれない)。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from src.eventnews.identity.focus import CLASS_CONFLICT, CLASS_LINEAGE, CLASS_SUPPORT
from src.eventnews.merge import LinkVerdict, MergeGroup, VetoedLink, plan_merges
from src.eventnews.models import MemberArticle
from src.storage.run_history import RunHistoryRepository
from src.ui.services.eventnews_focus_job import build_link_veto, focus_gate_mode

_E = ("actor", "lazarus")
_F = ("cve", "cve-2026-1")
_T0 = datetime(2026, 10, 24, 6, 0, 0, tzinfo=UTC)
_SINCE = datetime(2026, 10, 1, tzinfo=UTC)

Veto = Callable[[str, str], LinkVerdict | None]


def _v(x: float, y: float) -> np.ndarray:
    v = np.array([x, y], dtype=float)
    out: np.ndarray = v / np.linalg.norm(v)
    return out


def _plan(
    link_veto: Veto | None = None,
    *,
    enforce: bool = True,
    vetoed_out: list[VetoedLink] | None = None,
) -> list[MergeGroup]:
    ents = {k: frozenset({_E, _F}) for k in ("a1", "a2", "b1", "b2")}
    vecs = {"a1": _v(1, 0), "a2": _v(1, 0.01), "b1": _v(1, 0.02), "b2": _v(1, 0.03)}
    return plan_merges(
        item_of={"a1": "i1", "a2": "i1", "b1": "i2", "b2": "i2"},
        first_seen={"i1": "2026-08-01", "i2": "2026-08-10"},
        entities=ents,
        vectors=vecs,
        link_veto=link_veto,
        veto_enforce=enforce,
        vetoed_out=vetoed_out,
    )


class TestPlanMergesLinkVeto:
    def test_conflict_link_is_not_merged_and_is_recorded(self) -> None:
        out: list[VetoedLink] = []

        got = _plan(lambda a, b: LinkVerdict(CLASS_CONFLICT, "cve"), vetoed_out=out)

        assert got == []
        assert [(v.item_a, v.item_b, v.klass, v.enforced) for v in out] == [
            ("i1", "i2", CLASS_CONFLICT, True)
        ]
        assert out[0].edge_count == 4

    def test_shadow_records_the_conflict_but_still_merges(self) -> None:
        out: list[VetoedLink] = []

        got = _plan(lambda a, b: LinkVerdict(CLASS_CONFLICT, ""), enforce=False, vetoed_out=out)

        assert len(got) == 1
        assert out[0].enforced is False

    def test_lineage_is_never_blocked_but_is_kept_as_a_relation_candidate(self) -> None:
        out: list[VetoedLink] = []

        got = _plan(lambda a, b: LinkVerdict(CLASS_LINEAGE, "derivative"), vetoed_out=out)

        assert len(got) == 1
        assert [(v.klass, v.enforced) for v in out] == [(CLASS_LINEAGE, False)]

    def test_support_passes_without_a_record(self) -> None:
        out: list[VetoedLink] = []

        got = _plan(lambda a, b: LinkVerdict(CLASS_SUPPORT, ""), vetoed_out=out)

        assert len(got) == 1
        assert out == []

    def test_none_verdict_passes(self) -> None:
        assert len(_plan(lambda a, b: None)) == 1

    def test_no_veto_function_changes_nothing(self) -> None:
        assert len(_plan()) == 1

    def test_veto_is_not_asked_for_pairs_that_would_not_merge_anyway(self) -> None:
        asked: list[tuple[str, str]] = []

        def veto(a: str, b: str) -> LinkVerdict | None:
            asked.append((a, b))
            return None

        # 辺 1 本の組 (min_edges=2 に届かない) は結ばれないので照合も呼ばない
        plan_merges(
            item_of={"a1": "i1", "b1": "i2"},
            first_seen={"i1": "1", "i2": "2"},
            entities={"a1": frozenset({_E, _F}), "b1": frozenset({_E, _F})},
            vectors={"a1": _v(1, 0), "b1": _v(1, 0.01)},
            link_veto=veto,
        )

        assert asked == []


def _member(aid: str, *names: tuple[str, str]) -> MemberArticle:
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
        anchor_ts=_T0,
        summary="s",
        body="b",
        entities=frozenset(names),
    )


class TestBuildLinkVeto:
    def test_different_trusted_cves_give_conflict(self) -> None:
        members = {
            "i1": [_member("a1", ("cve", "CVE-2026-0001"))],
            "i2": [_member("b1", ("cve", "CVE-2026-0002"))],
        }
        veto = build_link_veto(lambda i: members[i], {})

        got = veto("i1", "i2")

        assert got is not None
        assert got.klass == CLASS_CONFLICT
        assert "cve" in got.detail

    def test_org_alias_removes_the_victim_conflict(self) -> None:
        members = {
            "i1": [_member("a1", ("victim_org", "acme-bank"))],
            "i2": [_member("b1", ("victim_org", "zenith-group"))],
        }
        alias = {"acme-bank": "k", "zenith-group": "k"}

        without = build_link_veto(lambda i: members[i], {})("i1", "i2")
        with_alias = build_link_veto(lambda i: members[i], alias)("i1", "i2")

        assert without is not None
        assert without.klass == CLASS_CONFLICT
        assert with_alias is None or with_alias.klass != CLASS_CONFLICT

    def test_shared_name_is_support_so_no_veto(self) -> None:
        members = {
            "i1": [_member("a1", ("cve", "CVE-2026-0001"))],
            "i2": [_member("b1", ("cve", "CVE-2026-0001"))],
        }
        assert build_link_veto(lambda i: members[i], {})("i1", "i2") is None

    def test_members_are_loaded_once_per_item(self) -> None:
        calls: list[str] = []
        members = {"i1": [_member("a1", _F)], "i2": [_member("b1", _F)]}

        def members_of(i: str) -> list[MemberArticle]:
            calls.append(i)
            return members[i]

        veto = build_link_veto(members_of, {})
        veto("i1", "i2")
        veto("i2", "i1")

        assert sorted(calls) == ["i1", "i2"]

    def test_failure_does_not_block(self) -> None:
        def boom(_: str) -> list[MemberArticle]:
            raise RuntimeError("db down")

        assert build_link_veto(boom, {})("i1", "i2") is None


class TestFocusGateMode:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, "on"),
            ("on", "on"),
            ("1", "on"),
            ("shadow", "shadow"),
            ("off", "off"),
            ("0", "off"),
        ],
    )
    def test_parses_flag(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: str
    ) -> None:
        if raw is None:
            monkeypatch.delenv("EVENTNEWS_MERGE_FOCUS_GATE", raising=False)
        else:
            monkeypatch.setenv("EVENTNEWS_MERGE_FOCUS_GATE", raw)

        assert focus_gate_mode() == expected

    def test_unknown_value_falls_back_to_shadow_not_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVENTNEWS_MERGE_FOCUS_GATE", "banana")

        assert focus_gate_mode() == "shadow"


class TestMergeVetoRecords:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> RunHistoryRepository:
        return RunHistoryRepository(db_path=tmp_path / "veto.db")

    def _veto(self, enforced: bool = True, klass: str = CLASS_CONFLICT) -> VetoedLink:
        return VetoedLink("i1", "i2", klass, "cve", 3, enforced)

    def test_repeated_hour_updates_times_seen_instead_of_adding_rows(
        self, repo: RunHistoryRepository
    ) -> None:
        repo.record_merge_vetoes([self._veto()], seen_at=_T0, mode="on")
        repo.record_merge_vetoes([self._veto()], seen_at=_T0.replace(hour=7), mode="on")

        rows = repo.list_merge_vetoes(since=_SINCE)

        assert len(rows) == 1
        assert rows[0].times_seen == 2
        assert rows[0].action == "blocked"
        assert rows[0].klass == CLASS_CONFLICT

    def test_action_reflects_mode_and_class(self, repo: RunHistoryRepository) -> None:
        repo.record_merge_vetoes(
            [
                VetoedLink("a", "b", CLASS_CONFLICT, "", 2, False),
                VetoedLink("c", "d", CLASS_LINEAGE, "derivative", 2, False),
            ],
            seen_at=_T0,
            mode="shadow",
        )

        by_pair = {(r.item_a, r.item_b): r.action for r in repo.list_merge_vetoes(since=_SINCE)}

        assert by_pair == {("a", "b"): "shadow", ("c", "d"): "relation_candidate"}

    def test_sampled_rows_are_excluded_from_next_sampling(self, repo: RunHistoryRepository) -> None:
        repo.record_merge_vetoes([self._veto()], seen_at=_T0, mode="on")

        assert repo.mark_merge_vetoes_sampled([("i1", "i2")], sampled_at=_T0) == 1

        assert repo.list_merge_vetoes(since=_SINCE, unsampled_only=True, actions=("blocked",)) == []
        assert len(repo.list_merge_vetoes(since=_SINCE)) == 1
