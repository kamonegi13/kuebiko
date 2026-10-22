"""事象どうしの統合 (2026-09-21)。

毎時の群化は「1 記事 × 1 事象」しか見ないため、事象 A の 3 本目と事象 B の 2 本目が
似ている関係に到達できない。実測の参加信号は seed 4,617 に対し既存への参加 146 (3%)。
"""

from __future__ import annotations

import numpy as np

from src.eventnews.merge import MergeGroup, candidate_pairs_by_entity, plan_merges

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


class TestFrequentEntityEdges:
    """頻出の名前を共有する組 (2026-10-02)。

    FBI のように窓内で頻出する名前は結合信号から外す (ハブ対策) が、大きく報じられた
    出来事ほどその名前でしか繋がらず、事象が割れていた (1 つの取引所の侵害だけで 10 事象超)。
    本文が十分似ていて ML が承認した組は、本数の規則とは別の辺として数える。
    測定 (14 日・本番の ML): 新たに承認 34 組、目視 30 組で誤り 0。
    """

    def test_extra_edge_merges_without_shared_capped_entity(self) -> None:
        got = plan_merges(
            item_of={"a": "i1", "b": "i2"},
            first_seen={"i1": "2026-09-01", "i2": "2026-09-10"},
            entities={"a": frozenset(), "b": frozenset()},  # 頻出ガード後は共有なし
            vectors={"a": _v(1, 0), "b": _v(1, 0.05)},
            approved=[],
            extra_edges=[("a", "b")],
        )

        assert [(g.target, g.absorbed) for g in got] == [("i1", ("i2",))]

    def test_extra_edge_between_two_multi_member_items_still_needs_two(self) -> None:
        item_of = {"a1": "i1", "a2": "i1", "b1": "i2", "b2": "i2"}
        got = plan_merges(
            item_of=item_of,
            first_seen={"i1": "2026-09-01", "i2": "2026-09-10"},
            entities={k: frozenset() for k in item_of},
            vectors={k: _v(1, 0.01 * n) for n, k in enumerate(item_of)},
            approved=[],
            extra_edges=[("a1", "b1")],
        )

        assert got == []


def test_frequent_entity_pairs_requires_cos_cross_item_and_no_roundup() -> None:
    from src.eventnews.merge import frequent_entity_pairs

    fbi = ("victim_org", "fbi")
    got = frequent_entity_pairs(
        uncapped={"a": {fbi}, "b": {fbi}, "c": {fbi}, "d": {fbi}, "r": {fbi}},
        capped={"a": frozenset(), "b": frozenset(), "c": frozenset(), "d": frozenset()},
        item_of={"a": "i1", "b": "i2", "c": "i3", "d": "i1", "r": "i4"},
        vectors={
            "a": _v(1, 0),
            "b": _v(1, 0.1),  # cos 高い・別事象 → 候補
            "c": _v(0, 1),  # cos 低い → 外す
            "d": _v(1, 0.05),  # a と同じ事象 (a-d は外す / b-d は別事象なので候補)
            "r": _v(1, 0.02),
        },
        roundups={"r"},
        threshold=0.80,
    )

    assert got == {("a", "b"), ("b", "d")}


class TestCveDisjointGate:
    """別の CVE を主題にする同ベンダ勧告どうしを統合しない (2026-10-12)。"""

    _ITEM_OF = {"a1": "i1", "a2": "i1", "b1": "i2", "b2": "i2"}
    _FIRST = {"i1": "2026-09-01", "i2": "2026-09-10"}
    _VECS = {"a1": _v(1, 0), "a2": _v(1, 0.01), "b1": _v(1, 0.02), "b2": _v(1, 0.03)}

    def _plan(self, ents: dict[str, frozenset[tuple[str, str]]], *, gate: bool) -> list[MergeGroup]:
        return plan_merges(
            item_of=self._ITEM_OF,
            first_seen=self._FIRST,
            entities=ents,
            vectors=self._VECS,
            cve_disjoint_gate=gate,
        )

    @staticmethod
    def _ents(cves_a: set[str], cves_b: set[str]) -> dict[str, frozenset[tuple[str, str]]]:
        shared = ("victim_org", "examplevendor")
        side_a = frozenset({shared, *(("cve", c) for c in cves_a)})
        side_b = frozenset({shared, *(("cve", c) for c in cves_b)})
        return {"a1": side_a, "a2": side_a, "b1": side_b, "b2": side_b}

    def test_blocks_merge_when_focal_cves_do_not_overlap(self) -> None:
        ents = self._ents({"cve-2026-1111"}, {"cve-2026-2222"})

        assert len(self._plan(ents, gate=False)) == 1  # 既定 off は従来どおり
        assert self._plan(ents, gate=True) == []

    def test_allows_merge_when_a_cve_is_shared(self) -> None:
        ents = self._ents({"cve-2026-1111"}, {"cve-2026-1111"})

        assert len(self._plan(ents, gate=True)) == 1

    def test_bulk_advisory_articles_do_not_count_as_focal(self) -> None:
        """CVE を FOCAL_CVE_MAX 超 列挙する一括勧告は主題ではないので、ゲートの根拠にしない。"""
        ents = self._ents({"cve-2026-1111"}, {f"cve-2026-{n}" for n in range(50)})

        assert len(self._plan(ents, gate=True)) == 1

    def test_no_cve_on_one_side_never_blocks(self) -> None:
        ents = self._ents({"cve-2026-1111"}, set())

        assert len(self._plan(ents, gate=True)) == 1


class TestBulletinBridgeGate:
    """一括の勧告 (CVE 4 件以上) と焦点の記事を CVE だけで結ぶ辺を使わない (既定 off)。"""

    @staticmethod
    def _plan(ents: dict[str, frozenset[tuple[str, str]]], *, gate: bool) -> list[MergeGroup]:
        vecs = {k: _v(1, 0.01 * i) for i, k in enumerate(ents)}
        return plan_merges(
            item_of={"a1": "i1", "a2": "i1", "b1": "i2", "b2": "i2"},
            first_seen={"i1": "2026-09-01", "i2": "2026-09-10"},
            entities=ents,
            vectors=vecs,
            bulletin_bridge_gate=gate,
        )

    @staticmethod
    def _cves(*ids: int) -> frozenset[tuple[str, str]]:
        return frozenset(("cve", f"cve-2026-{i}") for i in ids)

    def test_blocks_edges_between_a_bulletin_and_a_focal_article(self) -> None:
        ents = {
            "a1": self._cves(1, 2, 3, 4, 5),
            "a2": self._cves(1, 2, 3, 4, 5),
            "b1": self._cves(1),
            "b2": self._cves(2),
        }

        assert len(self._plan(ents, gate=False)) == 1
        assert self._plan(ents, gate=True) == []

    def test_keeps_edges_between_focal_articles(self) -> None:
        ents = {k: self._cves(1) for k in ("a1", "a2", "b1", "b2")}

        assert len(self._plan(ents, gate=True)) == 1

    def test_keeps_edges_between_two_bulletins_about_the_same_advisories(self) -> None:
        ents = {k: self._cves(1, 2, 3, 4, 5) for k in ("a1", "a2", "b1", "b2")}

        assert len(self._plan(ents, gate=True)) == 1

    def test_keeps_edges_that_share_a_non_cve_basis(self) -> None:
        victim = ("victim_org", "acme")
        ents = {
            "a1": self._cves(1, 2, 3, 4, 5) | {victim},
            "a2": self._cves(1, 2, 3, 4, 5) | {victim},
            "b1": self._cves(1) | {victim},
            "b2": self._cves(2) | {victim},
        }

        assert len(self._plan(ents, gate=True)) == 1


class TestHubGate:
    """複数 CVE を並記する大きな事象 (ハブ) の主題でない CVE だけを根拠に吸収しない (既定 off)。"""

    @staticmethod
    def _cves(*ids: int) -> frozenset[tuple[str, str]]:
        return frozenset(("cve", f"cve-2026-{i}") for i in ids)

    def _hub(self, size: int = 10) -> dict[str, frozenset[tuple[str, str]]]:
        """CVE 1 が主題で、先頭 4 本が別の CVE (5-8) を 1 つずつ並記するハブ。"""
        ents = {f"h{i}": self._cves(1) for i in range(size)}
        for i, minor in enumerate((5, 6, 7, 8)):
            ents[f"h{i}"] = self._cves(1, minor)
        return ents

    def _plan(self, absorbed_cve: int, *, gate: bool, hub_size: int = 10) -> list[MergeGroup]:
        ents = self._hub(hub_size)
        item_of = dict.fromkeys(ents, "hub")
        for b in ("b1", "b2"):
            ents[b] = self._cves(absorbed_cve)
            item_of[b] = "small"
        vecs = {k: _v(1, 0.01 * i) for i, k in enumerate(ents)}
        return plan_merges(
            item_of=item_of,
            first_seen={"hub": "2026-09-01", "small": "2026-09-10"},
            entities=ents,
            vectors=vecs,
            hub_gate=gate,
        )

    def test_blocks_absorbing_an_item_through_a_minor_cve_of_the_hub(self) -> None:
        assert len(self._plan(5, gate=False)) == 1  # 既定 off は従来どおり
        assert self._plan(5, gate=True) == []

    def test_keeps_merging_through_the_hubs_focal_cve(self) -> None:
        assert len(self._plan(1, gate=True)) == 1

    def test_small_items_are_not_hubs(self) -> None:
        """記事数が少ない事象は並記があっても制限しない (大きさだけで縛らない)。"""
        assert len(self._plan(5, gate=True, hub_size=5)) == 1

    def test_hub_without_many_cves_is_not_gated(self) -> None:
        """CVE の種類が FOCAL_CVE_MAX 以下の大きな事象は、主題が明確なので制限しない。"""
        ents = {f"h{i}": self._cves(1, 2) for i in range(10)}
        item_of = dict.fromkeys(ents, "hub")
        for b in ("b1", "b2"):
            ents[b] = self._cves(2)
            item_of[b] = "small"
        vecs = {k: _v(1, 0.01 * i) for i, k in enumerate(ents)}

        got = plan_merges(
            item_of=item_of,
            first_seen={"hub": "2026-09-01", "small": "2026-09-10"},
            entities=ents,
            vectors=vecs,
            hub_gate=True,
        )

        assert len(got) == 1

    def test_keeps_edges_that_share_a_non_cve_basis(self) -> None:
        ents = self._hub()
        victim = ("victim_org", "acme")
        ents = {k: v | {victim} for k, v in ents.items()}
        item_of = dict.fromkeys(ents, "hub")
        for b in ("b1", "b2"):
            ents[b] = self._cves(5) | {victim}
            item_of[b] = "small"
        vecs = {k: _v(1, 0.01 * i) for i, k in enumerate(ents)}

        got = plan_merges(
            item_of=item_of,
            first_seen={"hub": "2026-09-01", "small": "2026-09-10"},
            entities=ents,
            vectors=vecs,
            hub_gate=True,
        )

        assert len(got) == 1


class TestReportPeriodGate:
    """同じ種類の月次レポートで月が違うものを統合しない (既定 off)。"""

    @staticmethod
    def _plan(periods: dict[str, str], *, gate: bool) -> list[MergeGroup]:
        shared = frozenset({("malware", "foo"), ("tool", "bar")})
        ents = dict.fromkeys(("a1", "a2", "b1", "b2"), shared)
        vecs = {k: _v(1, 0.01 * i) for i, k in enumerate(ents)}
        return plan_merges(
            item_of={"a1": "i1", "a2": "i1", "b1": "i2", "b2": "i2"},
            first_seen={"i1": "2026-09-01", "i2": "2026-09-10"},
            entities=ents,
            vectors=vecs,
            report_periods=periods if gate else None,
        )

    def test_blocks_edges_between_reports_of_different_months(self) -> None:
        periods = {"a1": "2026-07", "a2": "2026-07", "b1": "2026-08", "b2": "2026-08"}

        assert len(self._plan(periods, gate=False)) == 1
        assert self._plan(periods, gate=True) == []

    def test_keeps_edges_between_reports_of_the_same_month(self) -> None:
        periods = dict.fromkeys(("a1", "a2", "b1", "b2"), "2026-08")

        assert len(self._plan(periods, gate=True)) == 1

    def test_keeps_edges_when_one_side_is_not_a_periodic_report(self) -> None:
        periods = {"a1": "2026-07", "a2": "2026-07"}

        assert len(self._plan(periods, gate=True)) == 1
