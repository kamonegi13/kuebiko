"""検索改善 (hybrid + rerank + planner) のテスト。LLM/embedder はフェイク。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from src.search.models import LlmQueryPlan, RerankItem, RerankOutput, SearchFacets
from src.search.service import _build_retrieval_plan, _detect_entity, search
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.tools.llm_client import LLMClient

if TYPE_CHECKING:
    from src.cti.actor_normalizer import ActorAliasRegistry


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "search.db")


def _seed(  # noqa: PLR0913
    repo: RunHistoryRepository,
    *,
    aid: str,
    title: str,
    when: datetime,
    actor: str | None = None,
    category: str = "apt",
    importance: str = "medium",
    malware: str | None = None,
) -> None:
    rid = repo.start_run(RunRecord(started_at=when, pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=title,
            url=f"https://e/{aid}",
            category=category,
            importance=importance,
            status="posted",
            created_at=when,
        )
    )
    ents: list[tuple[str, str]] = []
    if actor:
        ents.append(("actor", actor))
    if malware:
        ents.append(("malware_family", malware))
    if ents:
        repo.add_article_entities(aid, ents, when=when)


class _FakeLLM:
    """planner→LlmQueryPlan、reranker→RerankOutput を返すフェイク。"""

    model = "fake-llm"

    def __init__(self, plan: LlmQueryPlan | None = None, scores: dict[int, int] | None = None):
        self._plan = plan or LlmQueryPlan(semantic_query="通信侵入", keywords=["通信"])
        self._scores = scores or {}

    async def generate_structured(self, prompt: str, schema: type[Any], **_: Any) -> Any:
        if schema is LlmQueryPlan:
            return self._plan
        if schema is RerankOutput:
            # prompt 中の候補数を数える ([0] [1] ... の出現)
            import re

            n = len(re.findall(r"^\[(\d+)\]", prompt, re.MULTILINE))
            items = [
                RerankItem(index=i, score=self._scores.get(i, 5), reason="r") for i in range(n)
            ]
            return RerankOutput(items=items)
        return schema()


# ===================== _detect_entity =====================


class TestDetectEntity:
    def test_cve(self) -> None:
        assert _detect_entity("CVE-2024-3400") == ("cve", "CVE-2024-3400")

    def test_ip(self) -> None:
        assert _detect_entity("1.2.3.4") == ("ioc_ip", "1.2.3.4")

    def test_free_text_none(self) -> None:
        assert _detect_entity("中国の通信事業者侵入") is None

    def test_domain(self) -> None:
        assert _detect_entity("evil.example.com") == ("ioc_domain", "evil.example.com")


class TestBuildPlan:
    def test_quick_uses_raw(self) -> None:
        p = _build_retrieval_plan("CVE-2024-3400", None)
        assert p.keywords == ["CVE-2024-3400"]
        assert ("cve", "CVE-2024-3400") in p.entity_hints

    def test_precise_merges_llm_plan(self) -> None:
        plan = LlmQueryPlan(
            semantic_query="sq",
            keywords=["通信", "telecom"],
            categories=["apt"],
            actors=["lazarus"],
        )
        p = _build_retrieval_plan("中国の通信侵入", plan)
        assert p.semantic_query == "sq"
        assert "telecom" in p.keywords
        assert p.category_hints == ["apt"]
        assert ("actor", "lazarus") in p.entity_hints


# ===================== service.search =====================


class TestSearch:
    @pytest.mark.asyncio
    async def test_quick_keyword_hybrid(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        _seed(repo, aid="a1", title="中国APTが通信網に侵入", when=now)
        _seed(repo, aid="a2", title="農業遺伝資源の窃取", when=now - timedelta(hours=1))
        res = await search(repo, query="通信", mode="quick", embedder=None, llm=None)
        ids = [h.article_id for h in res.results]
        assert "a1" in ids  # keyword「通信」が a1 にヒット
        assert res.reranked is False
        assert res.mode == "quick"

    @pytest.mark.asyncio
    async def test_precise_rerank_reorders(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        # 両方「通信」で候補化。retrieval 順は created_at desc (a1, a2)。
        _seed(repo, aid="a1", title="通信記事 新", when=now)
        _seed(repo, aid="a2", title="通信記事 旧", when=now - timedelta(hours=2))
        # fake rerank: index0=2, index1=9 → 並べ替えで index1 が先頭に
        llm = _FakeLLM(
            plan=LlmQueryPlan(semantic_query="通信", keywords=["通信"]), scores={0: 2, 1: 9}
        )
        res = await search(
            repo, query="通信侵入", mode="precise", embedder=None, llm=cast(LLMClient, llm)
        )
        assert res.reranked is True
        assert len(res.results) >= 2
        # rerank 最高スコアが先頭
        assert res.results[0].rerank_score == max(h.rerank_score or -1 for h in res.results)
        assert res.results[0].rerank_score == 9

    @pytest.mark.asyncio
    async def test_precise_degrades_without_llm(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        _seed(repo, aid="a1", title="通信侵入の記事", when=now)
        # llm=None → precise でも rerank されず hybrid 結果
        res = await search(repo, query="通信", mode="precise", embedder=None, llm=None)
        assert res.reranked is False
        assert any(h.article_id == "a1" for h in res.results)

    @pytest.mark.asyncio
    async def test_empty_query(self, repo: RunHistoryRepository) -> None:
        res = await search(repo, query="   ", mode="quick", embedder=None, llm=None)
        assert res.count == 0


# ===================== facet 合成 (browse facet を search に AND) =====================


class TestSearchFacets:
    @pytest.mark.asyncio
    async def test_importance_hard_filter(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        _seed(repo, aid="hi", title="通信網に侵入 重大", when=now, importance="high")
        _seed(repo, aid="lo", title="通信網に侵入 軽微", when=now, importance="low")
        # facet importance=high → low の候補は post-filter で除去される
        facets = SearchFacets(importance="high")
        res = await search(repo, query="通信", mode="quick", embedder=None, llm=None, facets=facets)
        ids = [h.article_id for h in res.results]
        assert "hi" in ids
        assert "lo" not in ids

    @pytest.mark.asyncio
    async def test_category_group_facet(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        _seed(repo, aid="v", title="脆弱性 通信機器", when=now, category="vulnerability")
        _seed(repo, aid="a", title="APT 通信侵入", when=now, category="apt")
        facets = SearchFacets(category_in=("vulnerability", "advisory"))
        res = await search(repo, query="通信", mode="quick", embedder=None, llm=None, facets=facets)
        ids = [h.article_id for h in res.results]
        assert ids == ["v"]

    @pytest.mark.asyncio
    async def test_entity_facet_intersects_query(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        _seed(repo, aid="m1", title="通信網への攻撃", when=now, malware="lockbit")
        _seed(repo, aid="m2", title="通信網への攻撃 別件", when=now, malware="alphv")
        # query「通信」+ malware=lockbit → m2 は entity facet 外なので除去
        facets = SearchFacets(entity_filters=(("malware_family", ("lockbit",)),))
        res = await search(repo, query="通信", mode="quick", embedder=None, llm=None, facets=facets)
        ids = [h.article_id for h in res.results]
        assert "m1" in ids
        assert "m2" not in ids

    @pytest.mark.asyncio
    async def test_multiple_entity_filters_are_anded(self, repo: RunHistoryRepository) -> None:
        now = datetime.now(UTC)
        # a1 = lockbit + pir_x、a2 = lockbit のみ → cve... ここでは malware + pir の AND を検証
        _seed(repo, aid="a1", title="通信網 侵入", when=now, malware="lockbit")
        repo.add_article_entities("a1", [("pir", "pir_x")], when=now)
        _seed(repo, aid="a2", title="通信網 侵入 別", when=now, malware="lockbit")
        facets = SearchFacets(
            entity_filters=(("malware_family", ("lockbit",)), ("pir", ("pir_x",))),
        )
        res = await search(repo, query="通信", mode="quick", embedder=None, llm=None, facets=facets)
        ids = [h.article_id for h in res.results]
        assert ids == ["a1"]  # a2 は pir_x を持たないので AND で除去

    @pytest.mark.asyncio
    async def test_entity_filter_empty_set_yields_none(self, repo: RunHistoryRepository) -> None:
        # affected_vendor no-match 番兵相当: 該当 0 件の entity filter → 結果も 0 件
        now = datetime.now(UTC)
        _seed(repo, aid="a1", title="通信網 侵入", when=now)
        facets = SearchFacets(entity_filters=(("cve", ("__no_cve_for_vendor__",)),))
        res = await search(repo, query="通信", mode="quick", embedder=None, llm=None, facets=facets)
        assert res.count == 0

    @pytest.mark.asyncio
    async def test_no_facets_unchanged(self, repo: RunHistoryRepository) -> None:
        # facets=None は従来挙動 (post-filter 無し) を維持する
        now = datetime.now(UTC)
        _seed(repo, aid="x", title="通信侵入", when=now, importance="low")
        res = await search(repo, query="通信", mode="quick", embedder=None, llm=None, facets=None)
        assert any(h.article_id == "x" for h in res.results)


# ===================== actor 別名解決 (entity hint) =====================


class TestActorAliasResolution:
    """actor 別名 → canonical id 解決 (article_entities の actor は id 保存のため)。"""

    @staticmethod
    def _registry() -> ActorAliasRegistry:
        from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry

        return ActorAliasRegistry(
            actors=(ActorAlias(id="apt10", canonical="APT10", aliases=("Cicada", "Stone Panda")),),
        )

    def test_query_alias_resolves_to_actor_id_in_quick_mode(self) -> None:
        p = _build_retrieval_plan("Cicada の最新動向", None, registry=self._registry())
        assert ("actor", "apt10") in p.entity_hints

    def test_llm_actor_alias_resolves_to_actor_id(self) -> None:
        plan = LlmQueryPlan(semantic_query="sq", keywords=["k"], actors=["Stone Panda"])
        p = _build_retrieval_plan("無関係なクエリ", plan, registry=self._registry())
        assert ("actor", "apt10") in p.entity_hints
        assert ("actor", "stone panda") not in p.entity_hints

    def test_unresolved_actor_name_passes_raw(self) -> None:
        plan = LlmQueryPlan(semantic_query="sq", keywords=["k"], actors=["MysteryCat"])
        p = _build_retrieval_plan("query", plan, registry=self._registry())
        assert ("actor", "mysterycat") in p.entity_hints
