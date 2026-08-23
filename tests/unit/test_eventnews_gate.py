"""識別子関門 (カタログ番号参照 + 対称照合) の不変条件。

設計 SSoT: docs/event_news_design.md §9 / src/tools/identifier_catalog.py の docstring。
旧実装 (生成側 regex 抽出 × 原文側 部分文字列検索) は非対称ゆえ誤判定が尽きなかったため、
2026-08-23 に番号参照 + 集合帰属へ全面改修した。ここではその不変条件を固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.eventnews.identifier_gate import render_allowed_identifiers, verify_draft
from src.eventnews.models import EventNewsDraft, FactItem, MemberArticle


def _member(
    article_id: str, *, title: str = "t", summary: str = "s", body: str = ""
) -> MemberArticle:
    return MemberArticle(
        article_id=article_id,
        title=title,
        url=f"https://e/{article_id}",
        feed_title="F",
        feed_url="https://f",
        host="e",
        importance="high",
        category="apt",
        status="posted",
        anchor_ts=datetime(2026, 8, 20, tzinfo=UTC),
        summary=summary,
        body=body,
        entities=frozenset(),
    )


class TestCatalogRendering:
    def test_catalog_lists_values_with_member_numbers(self) -> None:
        members = (_member("a", body="CVE-2026-1111 の脆弱性"), _member("b", body="修正版 2.10.5"))
        text = render_allowed_identifiers(members)
        assert "CVE-2026-1111" in text
        assert "2.10.5" in text
        assert "記事 [1]" in text
        assert "記事 [2]" in text

    def test_empty_catalog_is_explicit(self) -> None:
        assert "識別子はありません" in render_allowed_identifiers((_member("a", body="なし"),))


class TestPlaceholderResolution:
    def test_reference_is_resolved_to_real_value(self) -> None:
        members = (_member("a", body="CVE-2026-1111 を悪用"), _member("b", body="別記事"))
        draft = EventNewsDraft(
            headline="{I1} の悪用",
            bluf="b",
            facts=[FactItem(text="{I1} が悪用された。", source_index=1)],
        )
        r = verify_draft(draft, members)
        assert r.draft.facts[0].text == "CVE-2026-1111 が悪用された。"
        assert r.draft.headline == "CVE-2026-1111 の悪用"
        assert r.repaired_ids >= 2

    def test_unknown_reference_is_removed(self) -> None:
        """カタログに無い番号は創作 — 表記ごと落とす (実値へ化けさせない)。"""
        members = (_member("a", body="CVE-2026-1111"), _member("b", body="x"))
        draft = EventNewsDraft(
            headline="h",
            bluf="b",
            facts=[FactItem(text="{I99} が悪用された。", source_index=1)],
        )
        r = verify_draft(draft, members)
        assert "I99" not in r.draft.facts[0].text
        assert r.substituted_ids == 1


class TestLiteralIdentifiers:
    def test_literal_outside_catalog_is_substituted_for_strict_kinds(self) -> None:
        members = (_member("a", body="CVE-2026-1111"), _member("b", body="x"))
        draft = EventNewsDraft(
            headline="h",
            bluf="b",
            facts=[FactItem(text="CVE-2026-9999 も悪用された。", source_index=1)],
        )
        r = verify_draft(draft, members)
        assert "CVE-2026-9999" not in r.draft.facts[0].text
        assert "(原文参照)" in r.draft.facts[0].text

    def test_literal_inside_catalog_is_kept(self) -> None:
        """CJK に隣接していてもカタログ集合に在れば保持する (対称照合の要点)。"""
        members = (
            _member("a", body="重大漏洞CVE-2026-1111，影響2.10.4以前版本"),
            _member("b", body="x"),
        )
        draft = EventNewsDraft(
            headline="h",
            bluf="b",
            facts=[FactItem(text="CVE-2026-1111 は 2.10.4 以前に影響する。", source_index=1)],
        )
        r = verify_draft(draft, members)
        assert r.draft.facts[0].text == "CVE-2026-1111 は 2.10.4 以前に影響する。"
        assert r.substituted_ids == 0

    def test_loose_kinds_are_counted_not_destroyed(self) -> None:
        """版数・CVSS は表記の変種が無限 — 本文を壊さず計数のみ (§C)。"""
        members = (_member("a", body="CVE-2026-1111"), _member("b", body="x"))
        draft = EventNewsDraft(
            headline="h",
            bluf="b",
            facts=[FactItem(text="バージョン 9.9.9 に影響する。", source_index=1)],
        )
        r = verify_draft(draft, members)
        assert "9.9.9" in r.draft.facts[0].text
        assert r.substituted_ids == 0


class TestSourceIndexGate:
    def test_out_of_range_fact_is_dropped(self) -> None:
        members = (_member("a", body="x"), _member("b", body="y"))
        draft = EventNewsDraft(
            headline="h",
            bluf="b",
            facts=[
                FactItem(text="範囲外", source_index=5),
                FactItem(text="未指定", source_index=0),
                FactItem(text="正常", source_index=1),
            ],
        )
        r = verify_draft(draft, members)
        assert [f.text for f in r.draft.facts] == ["正常"]
        assert r.dropped_lines == 2

    def test_discrepancy_without_index_is_kept(self) -> None:
        """不在の主張は原理的に [N] を持てない — 落とさない。"""
        members = (_member("a", body="x"), _member("b", body="y"))
        draft = EventNewsDraft(
            headline="h",
            bluf="b",
            discrepancies=[FactItem(text="どの媒体も侵入経路を特定していない。", source_index=0)],
        )
        r = verify_draft(draft, members)
        assert len(r.draft.discrepancies) == 1
        assert r.dropped_lines == 0
