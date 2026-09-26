"""事象どうしの統合 (2026-09-21)。

毎時の群化は「1 記事 × 1 事象」しか見ないため、事象 A の 3 本目と事象 B の 2 本目が
似ている関係に到達できない。実測の参加信号は seed 4,617 に対し既存への参加 146 (3%)。
"""

from __future__ import annotations

import numpy as np

from src.eventnews.merge import candidate_pairs_by_entity, plan_merges

_E = ("actor", "lazarus")
_F = ("cve", "cve-2026-1")


def _v(x: float, y: float) -> np.ndarray:
    v = np.array([x, y], dtype=float)
    out: np.ndarray = v / np.linalg.norm(v)
    return out


class TestCandidatePairs:
    def test_pairs_only_articles_that_share_an_entity(self) -> None:
        ents = {"a": frozenset({_E}), "b": frozenset({_E}), "c": frozenset({_F})}
        vecs = {k: _v(1, 0) for k in ents}

        assert candidate_pairs_by_entity(ents, vectors=vecs) == {("a", "b")}

    def test_skips_hub_entities_that_would_explode(self) -> None:
        """「ランサムウェア」のような語は数千記事に付き、総当たりが爆発する。"""
        ents = {str(i): frozenset({_E}) for i in range(10)}
        vecs = {k: _v(1, 0) for k in ents}

        assert candidate_pairs_by_entity(ents, vectors=vecs, hub_cap=5) == set()

    def test_articles_without_a_vector_are_not_paired(self) -> None:
        ents = {"a": frozenset({_E}), "b": frozenset({_E})}

        assert candidate_pairs_by_entity(ents, vectors={"a": _v(1, 0)}) == set()


class TestPlanMerges:
    def test_merges_two_items_connected_by_two_edges(self) -> None:
        """⭐ **辺 1 本では結ばない**。一括勧告 (ハブ) は相手ごとに 1 本ずつ細い辺を
        張るので、1 本で結ぶと多数を吸い込む (実測: VMware が 82 事象・277 記事)。"""
        ents = {k: frozenset({_E, _F}) for k in ("a1", "a2", "b1", "b2")}
        vecs = {"a1": _v(1, 0), "a2": _v(1, 0.01), "b1": _v(1, 0.02), "b2": _v(1, 0.03)}

        got = plan_merges(
            item_of={"a1": "i1", "a2": "i1", "b1": "i2", "b2": "i2"},
            first_seen={"i1": "2026-09-01", "i2": "2026-09-10"},
            entities=ents,
            vectors=vecs,
        )

        assert len(got) == 1
        assert got[0].target == "i1"  # ⭐ 最初に立った事象が統合先 (URL が残る)
        assert got[0].absorbed == ("i2",)

    def test_a_single_edge_does_not_merge(self) -> None:
        """ハブ対策の本体 — 細い繋がり 1 本では統合しない。"""
        ents = {"a": frozenset({_E, _F}), "b": frozenset({_E, _F})}
        vecs = {"a": _v(1, 0), "b": _v(1, 0.05)}

        got = plan_merges(
            item_of={"a": "i1", "b": "i2"},
            first_seen={"i1": "2026-09-01", "i2": "2026-09-10"},
            entities=ents,
            vectors=vecs,
        )

        assert got == []

    def test_members_of_the_same_item_are_not_merged_with_themselves(self) -> None:
        ents = {"a": frozenset({_E, _F}), "b": frozenset({_E, _F})}
        vecs = {"a": _v(1, 0), "b": _v(1, 0.05)}

        got = plan_merges(
            item_of={"a": "i1", "b": "i1"},
            first_seen={"i1": "2026-09-01"},
            entities=ents,
            vectors=vecs,
        )

        assert got == []

    def test_ml_veto_blocks_a_pair_the_deterministic_rule_would_allow(self) -> None:
        """⭐ ML が承認した対だけを通す。決定論だけだと、まとめ記事やニュースレター
        どうしを潰す (2026-09-01 の全期間 dry-run で実際に出た)。"""
        ents = {"a": frozenset({_E, _F}), "b": frozenset({_E, _F})}
        vecs = {"a": _v(1, 0), "b": _v(1, 0.05)}
        ents = {k: frozenset({_E, _F}) for k in ("a1", "a2", "b1", "b2")}
        vecs = {"a1": _v(1, 0), "a2": _v(1, 0.01), "b1": _v(1, 0.02), "b2": _v(1, 0.03)}
        item_of = {"a1": "i1", "a2": "i1", "b1": "i2", "b2": "i2"}
        first = {"i1": "2026-09-01", "i2": "2026-09-10"}
        ok = [("a1", "b1"), ("a1", "b2"), ("a2", "b1"), ("a2", "b2")]

        approved = plan_merges(
            item_of=item_of, first_seen=first, entities=ents, vectors=vecs, approved=ok
        )
        vetoed = plan_merges(
            item_of=item_of, first_seen=first, entities=ents, vectors=vecs, approved=[]
        )

        assert len(approved) == 1
        assert vetoed == []

    def test_no_time_limit_is_applied(self) -> None:
        """⭐ 実測で無制限でも 30 日と同じ (1,038 対 1,015)。判定自体が十分に厳しく、
        時間の上限は安全弁として機能していない — 恣意的な定数を持たない。"""
        ents = {k: frozenset({_E, _F}) for k in ("a1", "a2", "b1", "b2")}
        vecs = {"a1": _v(1, 0), "a2": _v(1, 0.01), "b1": _v(1, 0.02), "b2": _v(1, 0.03)}

        got = plan_merges(
            item_of={"a1": "i1", "a2": "i1", "b1": "i2", "b2": "i2"},
            first_seen={"i1": "2020-01-01", "i2": "2026-09-10"},  # 6 年差
            entities=ents,
            vectors=vecs,
        )

        assert len(got) == 1

    def test_chains_transitively_into_one_group(self) -> None:
        ks = ("a1", "a2", "b1", "b2", "c1", "c2")
        ents = {k: frozenset({_E, _F}) for k in ks}
        vecs = {k: _v(1, 0.01 * i) for i, k in enumerate(ks)}

        got = plan_merges(
            item_of={"a1": "i1", "a2": "i1", "b1": "i2", "b2": "i2", "c1": "i3", "c2": "i3"},
            first_seen={"i1": "2026-09-01", "i2": "2026-09-02", "i3": "2026-09-03"},
            entities=ents,
            vectors=vecs,
        )

        assert len(got) == 1
        assert got[0].target == "i1"
        assert set(got[0].absorbed) == {"i2", "i3"}


class TestSingletonRescue:
    """記事 1 件の事象の救済 (2026-09-27)。

    相手が 1 件の事象なら辺は構造上 1 本しか張れず、「2 本以上」の規則では永久に統合されない。
    相関クラスタリングの盲検で、取り残された単独記事を群に入れる方向は 47/50 が正しく、
    まとめ系の特徴が立つ辺を除くと 40/41 だった。誤りはすべて日次ダイジェスト・複数被害のまとめ。
    """

    def _plan(self, single_edge_ok: set[tuple[str, str]], item_of: dict[str, str]):  # type: ignore[no-untyped-def]
        ents = {k: frozenset({_E, _F}) for k in item_of}
        vecs = {k: _v(1, 0.01 * i) for i, k in enumerate(item_of)}
        return plan_merges(
            item_of=item_of,
            first_seen={
                i: f"2026-09-{n + 1:02d}" for n, i in enumerate(sorted(set(item_of.values())))
            },
            entities=ents,
            vectors=vecs,
            approved=sorted(single_edge_ok),
            single_edge_ok=single_edge_ok,
        )

    def test_singleton_joins_on_one_strong_edge(self) -> None:
        got = self._plan({("a", "b")}, {"a": "i1", "b": "i2"})
        assert [(g.target, g.absorbed) for g in got] == [("i1", ("i2",))]

    def test_singleton_joins_a_larger_item(self) -> None:
        got = self._plan({("a1", "s")}, {"a1": "i1", "a2": "i1", "s": "i2"})
        assert [(g.target, g.absorbed) for g in got] == [("i1", ("i2",))]

    def test_singleton_bridging_two_items_is_not_rescued(self) -> None:
        """単独記事が 2 つの事象へ辺を持つときは救わない (2 群を橋渡しして連鎖させない)。"""
        got = self._plan(
            {("a1", "s"), ("b1", "s")},
            {"a1": "i1", "a2": "i1", "b1": "i3", "b2": "i3", "s": "i2"},
        )
        assert got == []

    def test_non_singleton_pairs_still_need_two_edges(self) -> None:
        got = self._plan({("a1", "b1")}, {"a1": "i1", "a2": "i1", "b1": "i2", "b2": "i2"})
        assert got == []

    def test_without_single_edge_ok_behaviour_is_unchanged(self) -> None:
        ents = {"a": frozenset({_E, _F}), "b": frozenset({_E, _F})}
        got = plan_merges(
            item_of={"a": "i1", "b": "i2"},
            first_seen={"i1": "2026-09-01", "i2": "2026-09-10"},
            entities=ents,
            vectors={"a": _v(1, 0), "b": _v(1, 0.05)},
            approved=[("a", "b")],
        )
        assert got == []
