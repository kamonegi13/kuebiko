"""統合の時間の上限 (安全弁) と、統合の根拠 (辺) の持ち回り (2026-10-22)。

- 09-21 に「判定が十分厳しいので時間の上限は不要」として外したが、判定が緩む方向
  (ML のずれ・ハブ) では上限が唯一の歯止めになる。記事対の時間差が上限を超える辺は使わない。
- 統合の判断を記録できるよう、``MergeGroup`` は結んだ事象の組とその辺 (``links``) を持つ。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from src.eventnews.merge import exceeds_time_gap, plan_merges

_E = ("actor", "lazarus")
_F = ("cve", "cve-2026-1")
_BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _v(x: float, y: float) -> np.ndarray:
    v = np.array([x, y], dtype=float)
    out: np.ndarray = v / np.linalg.norm(v)
    return out


def _inputs() -> tuple[
    dict[str, str], dict[str, str], dict[str, frozenset[tuple[str, str]]], dict[str, np.ndarray]
]:
    ents = {k: frozenset({_E, _F}) for k in ("a1", "a2", "b1", "b2")}
    vecs = {"a1": _v(1, 0), "a2": _v(1, 0.01), "b1": _v(1, 0.02), "b2": _v(1, 0.03)}
    item_of = {"a1": "i1", "a2": "i1", "b1": "i2", "b2": "i2"}
    first = {"i1": "2026-08-01", "i2": "2026-08-10"}
    return item_of, first, ents, vecs


class TestExceedsTimeGap:
    def test_true_when_the_articles_are_further_apart_than_the_limit(self) -> None:
        ts = {"a": _BASE, "b": _BASE + timedelta(days=61)}

        assert exceeds_time_gap(ts, "a", "b", timedelta(days=60)) is True

    def test_false_within_the_limit_regardless_of_order(self) -> None:
        ts = {"a": _BASE, "b": _BASE + timedelta(days=59)}

        assert exceeds_time_gap(ts, "b", "a", timedelta(days=60)) is False

    def test_false_when_no_limit_or_unknown_time(self) -> None:
        ts = {"a": _BASE}

        assert exceeds_time_gap(ts, "a", "b", timedelta(days=1)) is False
        assert exceeds_time_gap({"a": _BASE, "b": _BASE}, "a", "b", None) is False


class TestPlanMergesTimeGap:
    def test_edges_beyond_the_limit_do_not_merge(self) -> None:
        item_of, first, ents, vecs = _inputs()
        ts = {
            "a1": _BASE,
            "a2": _BASE,
            "b1": _BASE + timedelta(days=90),
            "b2": _BASE + timedelta(days=90),
        }

        got = plan_merges(
            item_of=item_of,
            first_seen=first,
            entities=ents,
            vectors=vecs,
            article_ts=ts,
            max_gap=timedelta(days=60),
        )

        assert got == []

    def test_edges_within_the_limit_still_merge(self) -> None:
        item_of, first, ents, vecs = _inputs()
        ts = dict.fromkeys(("a1", "a2", "b1", "b2"), _BASE)

        got = plan_merges(
            item_of=item_of,
            first_seen=first,
            entities=ents,
            vectors=vecs,
            article_ts=ts,
            max_gap=timedelta(days=60),
        )

        assert len(got) == 1

    def test_no_limit_keeps_the_old_unbounded_behaviour(self) -> None:
        item_of, first, ents, vecs = _inputs()
        ts = {
            "a1": _BASE,
            "a2": _BASE,
            "b1": _BASE + timedelta(days=400),
            "b2": _BASE + timedelta(days=400),
        }

        got = plan_merges(
            item_of=item_of, first_seen=first, entities=ents, vectors=vecs, article_ts=ts
        )

        assert len(got) == 1


class TestMergeLinks:
    def test_group_carries_the_item_pair_and_its_article_edges(self) -> None:
        item_of, first, ents, vecs = _inputs()

        got = plan_merges(item_of=item_of, first_seen=first, entities=ents, vectors=vecs)

        assert len(got) == 1
        (link,) = got[0].links
        assert {link.item_a, link.item_b} == {"i1", "i2"}
        assert link.rescued is False
        assert len(link.edges) == 4  # a×b の 2×2 すべてが辺
        assert all(a.startswith("a") and b.startswith("b") for a, b in link.edges)

    def test_singleton_rescue_is_flagged_on_the_link(self) -> None:
        ents = {"a": frozenset({_E, _F}), "b": frozenset({_E, _F})}
        vecs = {"a": _v(1, 0), "b": _v(1, 0.05)}

        got = plan_merges(
            item_of={"a": "i1", "b": "i2"},
            first_seen={"i1": "2026-08-01", "i2": "2026-08-10"},
            entities=ents,
            vectors=vecs,
            approved=[("a", "b")],
            single_edge_ok={("a", "b")},
        )

        assert len(got) == 1
        assert got[0].links[0].rescued is True
