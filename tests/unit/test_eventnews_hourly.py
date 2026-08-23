"""毎時運用の増分処理 — 復元と候補限定の不変条件。

設計 SSoT: docs/event_news_design.md §12/§14b。リプレイと本番で同じ
``runner.process_candidates`` を使うため、この層の責務は「状態の復元」と
「候補の限定」のみ。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import numpy as np

from src.eventnews.hourly import hydrate_open_items, run_hourly
from src.eventnews.models import ItemState, MemberArticle
from src.storage.repo_eventnews import EventNewsMixin


class _FakeRepo:
    def __init__(self, records: list[object]) -> None:
        self._records = records
        self.created: list[str] = []
        self.members: list[tuple[str, str]] = []
        self.updates: list[str] = []

    def list_event_items(self, **_: object) -> list[object]:
        return self._records

    def create_event_item(self, *, item_id: str, **_: object) -> None:
        self.created.append(item_id)

    def add_event_member(self, *, item_id: str, article_id: str, **_: object) -> None:
        self.members.append((item_id, article_id))

    def update_event_item(self, item_id: str, _fields: dict[str, object]) -> int:
        self.updates.append(item_id)
        return 1

    def record_event_version(self, **_: object) -> None:  # pragma: no cover - 生成なし経路
        raise AssertionError("生成は起きない想定")


class _Rec:
    def __init__(self, state: ItemState, merged_into: str | None = None) -> None:
        self.state = state
        self.merged_into = merged_into


def _member(aid: str, *, hours_ago: int = 0) -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=f"t-{aid}",
        url=f"https://e/{aid}",
        feed_title="F",
        feed_url="https://f",
        host="e",
        importance="high",
        category="apt",
        status="posted",
        anchor_ts=datetime.now(UTC) - timedelta(hours=hours_ago),
        summary="s",
        body="",
        entities=frozenset({("cve", "cve-2026-1111")}),
    )


def _state(item_id: str, *, hours_ago: int, members: tuple[str, ...]) -> ItemState:
    ts = datetime.now(UTC) - timedelta(hours=hours_ago)
    return ItemState(
        item_id=item_id,
        first_reported_at=ts,
        last_reported_at=ts,
        status="new",
        importance="high",
        current_version=1,
        member_ids=members,
    )


class TestHydrate:
    def test_items_outside_the_window_are_not_hydrated(self) -> None:
        recent = _Rec(_state("ev-recent", hours_ago=1, members=("a",)))
        stale = _Rec(_state("ev-stale", hours_ago=24 * 30, members=("b",)))
        repo = _FakeRepo([recent, stale])

        out = hydrate_open_items(
            cast(EventNewsMixin, repo), lambda ids: {i: _member(i) for i in ids}
        )

        assert [s.item_id for s, _ in out] == ["ev-recent"]

    def test_merged_items_are_not_hydrated(self) -> None:
        """墓標 (merged_into) のアイテムは参加先にしない。"""
        rec = _Rec(_state("ev-old", hours_ago=1, members=("a",)), merged_into="ev-new")
        repo = _FakeRepo([rec])

        assert (
            hydrate_open_items(cast(EventNewsMixin, repo), lambda ids: {i: _member(i) for i in ids})
            == []
        )


class TestCandidateLimiting:
    def test_articles_already_members_are_skipped(self) -> None:
        """既存メンバーを候補に含めても二重処理しない (毎時実行の冪等性)。"""
        state = _state("ev-1", hours_ago=2, members=("a",))
        existing = [(state, [_member("a", hours_ago=2)])]
        repo = _FakeRepo([])
        cands = [_member("a", hours_ago=2), _member("b", hours_ago=1)]
        vectors = {"a": np.ones(4, dtype=np.float32), "b": np.ones(4, dtype=np.float32)}

        res = asyncio.run(run_hourly(cast(EventNewsMixin, repo), cands, vectors, existing, None))

        assert res.candidates == 1  # b のみ
        assert ("ev-1", "b") in repo.members  # 既存アイテムへ合流した
        assert repo.created == []  # 新規アイテムを作っていない
