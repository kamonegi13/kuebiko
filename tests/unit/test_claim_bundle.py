"""detect の束ね (別々の事案を 1 つの claim にまとめる) を見つける関門 (2026-09-24)。

⚠ **発端**: 5 日の replay で 4 モデルすべてが「日本国内の銀行 C・小売 B 社・中古販売 D 社…
で不正アクセスが相次いだ」のように、被害組織も事象も別の記事を 1 claim に束ねた (教師の Sonnet は
同じ出来事の複数報道しか束ねない)。台帳の割当を厳しくすると未割当が増え、この欠陥が表に出やすい。
記事どうしを「同じ事象 (群化) に入っている or 強い鍵を共有」で繋ぎ、塊が 2 つ以上なら束ね。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from src.assessment.claim_bundle import is_bundle, split_clusters
from src.synthesis.grounded.incremental import DetectResult


def test_same_event_articles_form_one_cluster() -> None:
    """同じ事象の複数報道 (FortiBleed を 5 媒体が報じた等) は束ねではない。"""
    clusters = split_clusters(
        ["a1", "a2", "a3"],
        strong_by_aid={"a1": {"cve:CVE-1"}, "a2": {"cve:CVE-1"}, "a3": set()},
        items_by_aid={"a3": {"e1"}, "a2": {"e1"}},
    )

    assert clusters == [["a1", "a2", "a3"]]
    assert is_bundle(clusters) is False


def test_different_victims_in_different_events_are_a_bundle() -> None:
    clusters = split_clusters(
        ["a1", "a2", "a3"],
        strong_by_aid={
            "a1": {"victim_org:bank-c"},
            "a2": {"victim_org:retailer-b"},
            "a3": {"victim_org:shop-d"},
        },
        items_by_aid={"a1": {"e1"}, "a2": {"e2"}, "a3": {"e3"}},
    )

    assert len(clusters) == 3
    assert is_bundle(clusters) is True


def test_clusters_are_largest_first_and_keep_the_input_order_inside() -> None:
    clusters = split_clusters(
        ["x", "a", "b"],
        strong_by_aid={"x": {"actor:z"}, "a": {"actor:y"}, "b": {"actor:y"}},
        items_by_aid={},
    )

    assert clusters == [["a", "b"], ["x"]]


def test_nations_do_not_join_articles() -> None:
    """国では繋がない (台帳の割当と同じく、国の共有は同一性ではない)。"""
    clusters = split_clusters(
        ["a1", "a2"],
        strong_by_aid={
            "a1": {"involved_country:US", "victim_org:x"},
            "a2": {"involved_country:US", "victim_org:y"},
        },
        items_by_aid={},
    )

    assert is_bundle(clusters) is True


def test_single_article_is_never_a_bundle() -> None:
    assert is_bundle(split_clusters(["a1"], strong_by_aid={}, items_by_aid={})) is False


class TestUnbundle:
    """束ねた claim は開設せず、塊ごとに claim を書き直させる (塊をまたいで束ねられない)。"""

    @staticmethod
    def _run(
        detected: DetectResult, rewrite: Callable[[list[str]], Awaitable[DetectResult]]
    ) -> DetectResult:
        import asyncio

        from src.assessment.claim_bundle import unbundle_claims

        strong = {
            "a1": {"victim_org:bank-c"},
            "a2": {"victim_org:retailer-b"},
            "b1": {"cve:CVE-1"},
            "b2": {"cve:CVE-1"},
        }
        return asyncio.run(
            unbundle_claims(detected, strong_by_aid=strong, items_by_aid={}, rewrite=rewrite)
        )

    def test_clean_claims_pass_through_without_rewriting(self) -> None:
        from src.synthesis.grounded.incremental import DetectedClaim, DetectResult

        calls: list[list[str]] = []

        async def rewrite(ids: list[str]) -> DetectResult:
            calls.append(ids)
            return DetectResult(open=(), rejected=(), overflow=0)

        ok = DetectedClaim(claim="CVE-1 の悪用", domain="cyber_incident", article_ids=("b1", "b2"))
        out = self._run(DetectResult(open=(ok,), rejected=(), overflow=0), rewrite)

        assert out.open == (ok,)
        assert calls == []

    def test_bundle_is_rewritten_per_cluster(self) -> None:
        from src.synthesis.grounded.incremental import DetectedClaim, DetectResult

        calls: list[list[str]] = []

        async def rewrite(ids: list[str]) -> DetectResult:
            calls.append(ids)
            return DetectResult(
                open=(
                    DetectedClaim(
                        claim=f"{ids[0]} の被害", domain="cyber_incident", article_ids=tuple(ids)
                    ),
                ),
                rejected=(),
                overflow=0,
            )

        bundle = DetectedClaim(
            claim="日本で相次ぐ", domain="cyber_incident", article_ids=("a1", "a2")
        )
        out = self._run(DetectResult(open=(bundle,), rejected=(), overflow=0), rewrite)

        assert calls == [["a1"], ["a2"]]
        assert [c.article_ids for c in out.open] == [("a1",), ("a2",)]

    def test_rewritten_claim_cannot_pull_in_articles_outside_its_cluster(self) -> None:
        from src.synthesis.grounded.incremental import DetectedClaim, DetectResult

        async def rewrite(ids: list[str]) -> DetectResult:
            return DetectResult(
                open=(DetectedClaim(claim="x", domain="d", article_ids=(*ids, "a2")),),
                rejected=(),
                overflow=0,
            )

        bundle = DetectedClaim(claim="束ね", domain="d", article_ids=("a1", "a2"))
        out = self._run(DetectResult(open=(bundle,), rejected=(), overflow=0), rewrite)

        assert all(set(c.article_ids) <= {"a1"} or set(c.article_ids) <= {"a2"} for c in out.open)

    def test_rewrite_failure_drops_only_that_cluster(self) -> None:
        """書き直しが失敗した塊は開設しない (記事は未割当に残り、次の run で候補になる)。"""
        from src.synthesis.grounded.incremental import DetectedClaim, DetectResult

        async def rewrite(ids: list[str]) -> DetectResult:
            if ids == ["a1"]:
                raise RuntimeError("LLM 不可")
            return DetectResult(
                open=(DetectedClaim(claim="y", domain="d", article_ids=tuple(ids)),),
                rejected=(),
                overflow=0,
            )

        bundle = DetectedClaim(claim="束ね", domain="d", article_ids=("a1", "a2"))
        out = self._run(DetectResult(open=(bundle,), rejected=(), overflow=0), rewrite)

        assert [c.article_ids for c in out.open] == [("a2",)]


def test_articles_without_keys_or_events_do_not_create_a_bundle() -> None:
    """⚠ 判断材料の無い記事 (固有名詞も事象も無い) では分けない — 最大の塊に含める。

    同じ出来事の報道でも、片方が未群化・固有名詞未抽出だと孤立する。これを束ねと誤判定すると
    書き直しで同じ事象の claim が 2 つでき、情勢が重複して開かれる。
    """
    clusters = split_clusters(
        ["a1", "a2", "bare"],
        strong_by_aid={"a1": {"cve:CVE-1"}, "a2": {"cve:CVE-1"}},
        items_by_aid={},
    )

    assert clusters == [["a1", "a2", "bare"]]
    assert is_bundle(clusters) is False


def test_near_identical_summaries_join_even_without_shared_keys() -> None:
    """同じ事象の報道で片方だけ固有名詞も事象も共有しない場合、要約の類似度 (≥0.75) で繋ぐ。

    実測: Rust の arrayref 汚染の 3 本は類似度 0.87 だが 1 本が鍵を共有せず分かれた。
    日本の各社の被害 (別事案) は書き方が似ていても最大 0.68 — 0.75 なら分かれたまま。
    """
    import numpy as np

    def v(*xs: float) -> np.ndarray:
        a = np.asarray(xs, dtype=np.float32)
        return a / np.linalg.norm(a)

    clusters = split_clusters(
        ["a1", "a2", "a3"],
        strong_by_aid={"a1": {"actor:x"}, "a2": {"actor:x"}, "a3": {"actor:y"}},
        items_by_aid={},
        vec_by_aid={"a1": v(1, 0), "a2": v(1, 0.1), "a3": v(1, 0.2)},
    )

    assert clusters == [["a1", "a2", "a3"]]


def test_similar_but_distinct_incidents_stay_apart() -> None:
    import numpy as np

    def v(*xs: float) -> np.ndarray:
        a = np.asarray(xs, dtype=np.float32)
        return a / np.linalg.norm(a)

    clusters = split_clusters(
        ["a1", "a2"],
        strong_by_aid={"a1": {"victim_org:x"}, "a2": {"victim_org:y"}},
        items_by_aid={},
        vec_by_aid={"a1": v(1, 0), "a2": v(0.65, 0.76)},  # 類似度 0.65
    )

    assert is_bundle(clusters) is True
