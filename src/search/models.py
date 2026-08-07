"""検索パイプラインのデータモデル (内部 + LLM 入出力 schema)。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SearchFacets(BaseModel):
    """query 駆動の retrieval に AND 合成する hard filter (UI の facet 由来)。

    検索 box の query が「何を探すか」(ranking/recall) を、ここに載る facet が
    「どこに絞るか」を担う (soft boost ではなく hard filter)。語彙は News サーフェスの
    browse facet (importance / category / feed / channel / intent / entity tag / 期間)
    と完全一致させ、browse と search で同じ絞り込みが効くようにする。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    importance: str | None = None
    importance_in: tuple[str, ...] = ()
    category: str | None = None
    category_in: tuple[str, ...] = ()
    feed_title: str | None = None
    posted_channel: str | None = None
    socio_political_intent: str | None = None
    # entity tag facet: (entity_type, values) の組を **AND 合成** (values 内は OR)。
    # cve / malware_family / pir / affected_vendor(→cve群) を同時に絞り込める。値は LOWER 一致。
    entity_filters: tuple[tuple[str, tuple[str, ...]], ...] = ()
    since: datetime | None = None
    status: str | None = "posted"
    # 本文の由来による絞り込み (2026-07-27): "stump"=フィード抜粋のみ(全文未取得) /
    # "full"=全文取得済。切り株記事を一覧するための facet。
    body_source: str | None = None

    def is_empty(self) -> bool:
        """status 以外の絞り込みが 1 つも無ければ True (status は baseline 扱い)。"""
        return not (
            self.importance
            or self.importance_in
            or self.category
            or self.category_in
            or self.feed_title
            or self.posted_channel
            or self.socio_political_intent
            or self.entity_filters
            or self.since is not None
            or self.body_source
        )

    def to_query_kwargs(self) -> dict[str, object]:
        """``RunHistoryRepository.list_articles`` に渡せる filter kwargs に変換。

        ``search`` / ``limit`` / ``offset`` は呼び出し側が付ける。この dict が
        list_articles と retriever leg / post-filter の SSoT になる (mapping の二重化回避)。
        """
        kw: dict[str, object] = {}
        if self.importance:
            kw["importance"] = self.importance
        if self.importance_in:
            kw["importance_in"] = list(self.importance_in)
        if self.category:
            kw["category"] = self.category
        if self.category_in:
            kw["category_in"] = list(self.category_in)
        if self.feed_title:
            kw["feed_title"] = self.feed_title
        if self.posted_channel:
            kw["posted_channel"] = self.posted_channel
        if self.socio_political_intent:
            kw["socio_political_intent"] = self.socio_political_intent
        if self.entity_filters:
            kw["entity_filters"] = [(t, list(vs)) for t, vs in self.entity_filters]
        if self.since is not None:
            kw["since"] = self.since
        if self.status:
            kw["status"] = self.status
        if self.body_source:
            kw["body_source"] = self.body_source
        return kw


class LlmQueryPlan(BaseModel):
    """D (planner) の LLM 出力。NL クエリ → 構造化検索プラン (全て hint = soft-boost)。"""

    model_config = ConfigDict(extra="ignore")

    semantic_query: str = ""  # 意味検索用に整えたクエリ
    keywords: list[str] = Field(default_factory=list)  # lexical 用キーワード
    categories: list[str] = Field(default_factory=list)  # category hint
    actors: list[str] = Field(default_factory=list)  # actor id/別名 hint
    cves: list[str] = Field(default_factory=list)
    intent: str = ""  # socio_political_intent hint


class RetrievalPlan(BaseModel):
    """retriever (B) に渡す内部プラン (planner 出力 + 自動検出を統合した正規形)。"""

    model_config = ConfigDict(extra="ignore")

    semantic_query: str = ""
    keywords: list[str] = Field(default_factory=list)
    category_hints: list[str] = Field(default_factory=list)
    entity_hints: list[tuple[str, str]] = Field(default_factory=list)  # (entity_type, value)
    intent_hint: str = ""


class RerankItem(BaseModel):
    index: int
    score: float = 0.0
    reason: str = ""


class RerankOutput(BaseModel):
    """C (reranker) の LLM 出力。候補 index ごとの関連度 0-10 + 理由。"""

    model_config = ConfigDict(extra="ignore")

    items: list[RerankItem] = Field(default_factory=list)


class SearchHit(BaseModel):
    """検索 1 件 (API 戻り)。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    article_id: str
    title: str
    url: str
    importance: str | None = None
    category: str | None = None
    feed_title: str | None = None
    socio_political_intent: str | None = None
    # 確度 (NULL=旧レジーム確定 / low=仮説)。intent チップの表示に使う
    intent_confidence: str | None = None
    summary: str | None = None
    created_at: str | None = None
    score: float = 0.0  # fused (RRF) score
    rerank_score: float | None = None  # C 採点 (precise のみ)
    reason: str = ""  # rerank の理由
    matched_via: list[str] = Field(default_factory=list)  # vector/keyword/structured


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str
    mode: str  # 'quick' | 'precise'
    count: int = 0
    results: list[SearchHit] = Field(default_factory=list)
    reranked: bool = False
    embeddings_available: bool = True
    plan: LlmQueryPlan | None = None
