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
    def test_merges_two_items_whose_members_match(self) -> None:
        ents = {"a": frozenset({_E, _F}), "b": frozenset({_E, _F})}
        vecs = {"a": _v(1, 0), "b": _v(1, 0.05)}  # cos ≈ 0.999

        got = plan_merges(
            item_of={"a": "i1", "b": "i2"},
            first_seen={"i1": "2026-09-01", "i2": "2026-09-10"},
            entities=ents,
            vectors=vecs,
        )

        assert len(got) == 1
        assert got[0].target == "i1"  # ⭐ 最初に立った事象が統合先 (URL が残る)
        assert got[0].absorbed == ("i2",)

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
        item_of = {"a": "i1", "b": "i2"}
        first = {"i1": "2026-09-01", "i2": "2026-09-10"}

        approved = plan_merges(
            item_of=item_of,
            first_seen=first,
            entities=ents,
            vectors=vecs,
            approved=[("a", "b")],
        )
        vetoed = plan_merges(
            item_of=item_of, first_seen=first, entities=ents, vectors=vecs, approved=[]
        )

        assert len(approved) == 1
        assert vetoed == []

    def test_no_time_limit_is_applied(self) -> None:
        """⭐ 実測で無制限でも 30 日と同じ (1,038 対 1,015)。判定自体が十分に厳しく、
        時間の上限は安全弁として機能していない — 恣意的な定数を持たない。"""
        ents = {"a": frozenset({_E, _F}), "b": frozenset({_E, _F})}
        vecs = {"a": _v(1, 0), "b": _v(1, 0.05)}

        got = plan_merges(
            item_of={"a": "i1", "b": "i2"},
            first_seen={"i1": "2020-01-01", "i2": "2026-09-10"},  # 6 年差
            entities=ents,
            vectors=vecs,
        )

        assert len(got) == 1

    def test_chains_transitively_into_one_group(self) -> None:
        ents = {k: frozenset({_E, _F}) for k in ("a", "b", "c")}
        vecs = {"a": _v(1, 0), "b": _v(1, 0.03), "c": _v(1, 0.06)}

        got = plan_merges(
            item_of={"a": "i1", "b": "i2", "c": "i3"},
            first_seen={"i1": "2026-09-01", "i2": "2026-09-02", "i3": "2026-09-03"},
            entities=ents,
            vectors=vecs,
        )

        assert len(got) == 1
        assert got[0].target == "i1"
        assert set(got[0].absorbed) == {"i2", "i3"}
