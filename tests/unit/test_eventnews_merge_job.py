"""事象どうしの統合を毎時の段として回し、本文再生成と対にする (2026-09-21)。

⚠ 統合先は ``current_version`` を 0 に戻す = 本文が消える。統合と再生成を別々に
呼べる設計にすると「統合だけ適用して本文なし」が再発する (2026-09-02 に 45 件)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import numpy as np
import pytest

from src.eventnews import pair_shadow
from src.eventnews.merge import MergeGroup
from src.eventnews.models import ItemState, MemberArticle
from src.storage.run_history import RunHistoryRepository
from src.ui.services import eventnews_merge_job as job

_E = ("actor", "lazarus")
_F = ("cve", "cve-2026-1")


def _member(aid: str, *, importance: str = "medium", feed: str = "F") -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=f"t-{aid}",
        url=f"https://{feed.lower()}.example/{aid}",
        feed_title=feed,
        feed_url=f"https://{feed.lower()}.example/feed",
        host=f"{feed.lower()}.example",
        importance=importance,
        category="apt",
        status="posted",
        anchor_ts=datetime(2026, 9, 1, tzinfo=UTC),
        summary="s",
        body="b" * 400,
        entities=frozenset({_E, _F}),
    )


def _state(item_id: str, members: tuple[str, ...], *, day: int, importance: str) -> ItemState:
    return ItemState(
        item_id=item_id,
        status="new",
        member_ids=members,
        first_reported_at=datetime(2026, 9, day, tzinfo=UTC),
        last_reported_at=datetime(2026, 9, day, tzinfo=UTC),
        current_version=1,
        importance=importance,
    )


class _Rec:
    def __init__(self, state: ItemState) -> None:
        self.state = state
        self.merged_into: str | None = None


class _FakeRepo:
    def __init__(self) -> None:
        self.members: list[tuple[str, str, str]] = []
        self.updates: list[tuple[str, dict[str, object]]] = []

    def add_event_member(
        self, *, item_id: str, article_id: str, join_signal: str, **_: object
    ) -> None:
        self.members.append((item_id, article_id, join_signal))

    def update_event_item(self, item_id: str, fields: dict[str, object]) -> int:
        self.updates.append((item_id, fields))
        return 1


def _v(x: float, y: float) -> np.ndarray:
    v = np.array([x, y], dtype=np.float32)
    out: np.ndarray = v / np.linalg.norm(v)
    return out


def _inputs() -> job.MergeInputs:
    members = {
        "a1": _member("a1", importance="low", feed="F"),
        "a2": _member("a2", importance="low", feed="G"),
        "b1": _member("b1", importance="high", feed="H"),
    }
    records = {
        "i1": _Rec(_state("i1", ("a1", "a2"), day=1, importance="low")),
        "i2": _Rec(_state("i2", ("b1",), day=10, importance="high")),
    }
    return job.MergeInputs(
        item_of={"a1": "i1", "a2": "i1", "b1": "i2"},
        first_seen={"i1": "2026-09-01", "i2": "2026-09-10"},
        members=members,
        entities={k: m.entities for k, m in members.items()},
        vectors={"a1": _v(1, 0), "a2": _v(1, 0.01), "b1": _v(1, 0.02)},
        summary_vectors={},
        records=cast(dict[str, job.EventItemRecord], records),
    )


class TestFlags:
    def test_flag_default_on_and_cap_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EVENTNEWS_MERGE", raising=False)
        monkeypatch.delenv("EVENTNEWS_MERGE_REGEN_CAP", raising=False)
        assert job.merge_enabled() is True
        assert job.regen_cap() == job.DEFAULT_REGEN_CAP

    def test_flag_off_and_cap_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVENTNEWS_MERGE", "0")
        monkeypatch.setenv("EVENTNEWS_MERGE_REGEN_CAP", "3")
        assert job.merge_enabled() is False
        assert job.regen_cap() == 3
        monkeypatch.setenv("EVENTNEWS_MERGE_REGEN_CAP", "abc")
        assert job.regen_cap() == job.DEFAULT_REGEN_CAP


class TestApprovePairs:
    def test_only_cross_item_pairs_are_scored_without_llm(self) -> None:
        """⭐ ML だけで判定する (LLM を対ごとに呼ぶと 59,356 対で 16 時間)。
        同じ事象の中の対 (a1, a2) は採点しない。"""
        inputs = _inputs()
        scored: list[tuple[str, str]] = []

        class _M:
            def joins(self, features: list[float], *, llm_available: bool) -> bool:
                assert llm_available is False
                assert features[-2:] == [0.0, 0.0]  # LLM 判定なしの埋め方
                return True

        approved = job.approve_pairs(inputs, cast(job.PairModel, _M()), on_pair=scored.append)

        assert set(scored) == {("a1", "b1"), ("a2", "b1")}
        assert set(approved) == {("a1", "b1"), ("a2", "b1")}


class TestApplyMergeGroup:
    def test_target_absorbs_members_and_loses_its_body(self) -> None:
        repo = _FakeRepo()
        inputs = _inputs()
        now = datetime(2026, 9, 21, tzinfo=UTC)

        absorbed = job.apply_merge_group(
            cast(RunHistoryRepository, repo), MergeGroup(target="i1", absorbed=("i2",)), inputs, now
        )

        assert absorbed == 1
        assert {(i, a) for i, a, _ in repo.members} == {("i1", "a1"), ("i1", "a2"), ("i1", "b1")}
        assert {s for _, _, s in repo.members} == {"retro_merge"}
        by_id = dict(repo.updates)
        target = by_id["i1"]
        assert target["current_version"] == 0  # ⚠ 本文が消える → 再生成と対で呼ぶ
        assert target["importance"] == "high"  # 吸収した側の方が高ければ引き上げる
        assert target["independent_sources"] == 3
        assert target["last_reported_at"] == datetime(2026, 9, 10, tzinfo=UTC)
        assert by_id["i2"] == {"merged_into": "i1", "updated_at": now}


class TestRunSkipsWhenMlNotReady:
    def test_deterministic_only_merge_never_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """⚠ 決定論だけで統合すると一括勧告が 82 事象を吸い込む (実測)。ML が
        使えないときは統合しない (本文の再生成だけは行う)。"""
        monkeypatch.setattr(pair_shadow, "is_ml_ready", lambda: False)
        loaded: list[str] = []
        monkeypatch.setattr(job, "load_merge_inputs", lambda repo: loaded.append("x"))

        result = job.plan_for(cast(RunHistoryRepository, object()))

        assert result.skipped == "ml_not_ready"
        assert result.groups == ()
        assert loaded == []


class TestRegenBudget:
    def test_budget_default_and_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EVENTNEWS_MERGE_REGEN_BUDGET_SECONDS", raising=False)
        assert job.regen_budget_seconds() == job.DEFAULT_REGEN_BUDGET_SECONDS
        monkeypatch.setenv("EVENTNEWS_MERGE_REGEN_BUDGET_SECONDS", "90")
        assert job.regen_budget_seconds() == 90

    def test_generate_pending_stops_before_next_item_when_budget_is_spent(self) -> None:
        """⭐ 上限は件数でなく **時間** でも掛かる。2026-09-21 に上限 5 件でも段が 20 分の
        timeout に当たった (1 件 10 分超)。次の件を始める前に予算を見る。"""
        import asyncio

        from src.eventnews.runner import generate_pending

        state = _state("i1", ("a1", "a2"), day=1, importance="low")
        calls: list[str] = []

        class _Llm:
            model = "fake"

        stats = asyncio.run(
            generate_pending(
                cast(RunHistoryRepository, _FakeRepo()),
                [(state, [_member("a1"), _member("a2")])],
                lambda: cast(Any, _Llm()),
                stop_when=lambda: True,
                on_progress=lambda i, n, iid: calls.append(iid),
            )
        )

        assert stats.attempted == 0 and stats.generated == 0
        assert calls == []


class TestSingletonRescueWiring:
    def test_roundup_pairs_are_not_single_edge_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """まとめ系の特徴が立つ承認対は、1 本での救済に使わない (誤り 3/3 がまとめ系)。"""
        from src.eventnews.pair_features import FEATURE_NAMES

        inputs = _inputs()
        idx = FEATURE_NAMES.index("roundup_one")

        def fake_features(left: object, right: object) -> list[float]:
            f = [0.0] * len(FEATURE_NAMES)
            # a2-b1 だけまとめ系
            if {getattr(left, "article_id", ""), getattr(right, "article_id", "")} == {"a2", "b1"}:
                f[idx] = 1.0
            return f

        monkeypatch.setattr(job, "pair_features", fake_features)

        class _M:
            def joins(self, features: list[float], *, llm_available: bool) -> bool:
                return True

        strong: set[tuple[str, str]] = set()
        job.approve_pairs(inputs, cast(job.PairModel, _M()), single_edge_ok=strong)
        assert strong == {("a1", "b1")}

    def test_rescue_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert job.singleton_rescue_enabled() is True
        monkeypatch.setenv("EVENTNEWS_SINGLETON_RESCUE", "0")
        assert job.singleton_rescue_enabled() is False
