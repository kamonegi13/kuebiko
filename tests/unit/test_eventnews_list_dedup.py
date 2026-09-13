"""事象ニュース draft のリスト欄 重複畳み込み (2026-09-11)。

1. 判定器: 同文 (空白差のみ) を先頭 1 件に畳み、順序を保つ / 近い別文は潰さない
2. 配線: generate_draft の戻りで畳まれる (通常生成・書き直しの双方)。重複なしなら同一オブジェクト
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from src.eventnews.generator import generate_draft
from src.eventnews.list_dedup import dedup_draft, dedup_items
from src.eventnews.models import EventNewsDraft, FactItem, MemberArticle
from src.tools.llm_client import LLMClient


class TestDedupItems:
    def test_exact_and_whitespace_duplicates_collapse_to_first(self) -> None:
        items = ["a b", "a  b", "c", "a b"]
        assert dedup_items(items) == ["a b", "c"]

    def test_near_duplicates_are_kept(self) -> None:
        # 意味の近さでは潰さない (近い別主張を消す誤りの方が重い)
        items = ["被害は 80 組織", "被害は 80 組織超"]
        assert dedup_items(items) == items

    def test_fact_items_keyed_by_text_only(self) -> None:
        a = FactItem(text="f", source_index=1)
        b = FactItem(text="f", source_index=2)  # 出典が違っても同文は反復とみなす
        assert dedup_items([a, b]) == [a]


class TestDedupDraft:
    def test_returns_same_object_when_clean(self) -> None:
        draft = EventNewsDraft(headline="h", bluf="b", facts=[FactItem(text="f", source_index=1)])
        out, removed = dedup_draft(draft)
        assert out is draft
        assert removed == {}

    def test_removes_across_all_list_fields(self) -> None:
        draft = EventNewsDraft(
            headline="h",
            bluf="b",
            key_points=["k", "k"],
            facts=[FactItem(text="f", source_index=1)] * 3,
            discrepancies=[FactItem(text="d", source_index=1), FactItem(text="d2", source_index=1)],
            caveats=[FactItem(text="c", source_index=1)] * 2,
            unknowns=["u"] * 5,
        )
        out, removed = dedup_draft(draft)
        assert removed == {"key_points": 1, "facts": 2, "caveats": 1, "unknowns": 4}
        assert out.key_points == ["k"]
        assert len(out.facts) == 1
        assert [d.text for d in out.discrepancies] == ["d", "d2"]
        assert out.unknowns == ["u"]
        assert out.headline == "h"  # 他欄は不変


class _FakeLLM:
    def __init__(self, draft: EventNewsDraft) -> None:
        self._draft = draft

    async def generate_structured(self, prompt: str, schema: type, **_kw: Any) -> EventNewsDraft:
        return self._draft


def _member(article_id: str) -> MemberArticle:
    return MemberArticle(
        article_id=article_id,
        title=f"title-{article_id}",
        url=f"https://kuebiko.example/{article_id}",
        feed_title="feed",
        feed_url="https://kuebiko.example/feed",
        host="kuebiko.example",
        importance="medium",
        category="apt",
        status="posted",
        anchor_ts=datetime(2026, 9, 1, tzinfo=UTC),
        summary="Actor abused CVE-2024-1234",
        body="",
        entities=frozenset(),
    )


class TestGenerateDraftWiring:
    def test_looped_unknowns_are_collapsed(self) -> None:
        draft = EventNewsDraft(headline="h", bluf="b", unknowns=["u1"] * 300 + ["u2"])
        result = asyncio.run(generate_draft([_member("a1")], "", cast(LLMClient, _FakeLLM(draft))))
        assert result.unknowns == ["u1", "u2"]

    def test_clean_draft_passes_through_unchanged(self) -> None:
        draft = EventNewsDraft(headline="h", bluf="b", unknowns=["u1", "u2"])
        result = asyncio.run(generate_draft([_member("a1")], "", cast(LLMClient, _FakeLLM(draft))))
        assert result is draft


class TestNearDuplicates:
    def test_paraphrase_variants_collapse(self) -> None:
        a = (
            "影響を受けるバージョンについて、一方は最新版まで、もう一方はそれ以前としており、"
            "表記の粒度が異なります。"
        )
        b = (
            "影響を受けるバージョン表記について、一方は最新版まで、もう一方はそれ以前としており、"
            "表記の粒度が異なります。"
        )
        assert dedup_items([a, b]) == [a]

    def test_numeric_difference_is_not_a_duplicate(self) -> None:
        # 日付・件数・版数の違いは別主張 (差分に数字があれば畳まない)
        a = "報告日について、一方は 2026 年 8 月 20 日とされ、もう一方は同日に報じられたと記載。"
        b = "報告日について、一方は 2026 年 8 月 21 日とされ、もう一方は同日に報じられたと記載。"
        assert dedup_items([a, b]) == [a, b]

    def test_short_claims_are_never_collapsed_by_similarity(self) -> None:
        assert dedup_items(["被害は 80 組織", "被害は 80 組織超"]) == [
            "被害は 80 組織",
            "被害は 80 組織超",
        ]

    def test_distinct_claims_are_kept(self) -> None:
        a = "初期侵入経路は特定されていない。"
        b = "窃取されたデータの内容は公表されていない。"
        assert dedup_items([a, b]) == [a, b]
