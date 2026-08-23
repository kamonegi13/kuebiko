"""事象単位ニュースの識別子関門 (src/eventnews/identifier_gate.py) のテスト。

範囲外 index の落下 / cross-member 転植の repair・置換 / 置換後も行が残ること /
本文空 (purge 済み) で保持されること / discrepancies が source_index=0 でも
残ることを固定する (docs/event_news_design.md §9)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.eventnews.identifier_gate import extract_allowed_by_member, verify_draft
from src.eventnews.models import EventNewsDraft, FactItem, MemberArticle

_NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _member(
    article_id: str,
    *,
    title: str = "title",
    summary: str = "summary",
    body: str = "body",
) -> MemberArticle:
    return MemberArticle(
        article_id=article_id,
        title=title,
        url=f"https://example.com/{article_id}",
        feed_title="feed",
        feed_url="https://example.com/feed",
        host="example.com",
        importance="medium",
        category="threat",
        status="posted",
        anchor_ts=_NOW,
        summary=summary,
        body=body,
        entities=frozenset(),
    )


# ---------- extract_allowed_by_member ----------


def test_extract_allowed_by_member_combines_title_and_body() -> None:
    member = _member("m1", title="APT99 の活動", summary="", body="CVE-2024-1111 を悪用")

    result = extract_allowed_by_member((member,))

    assert len(result) == 1
    kinds = {ident.kind for ident in result[0]}
    assert "actor_id" in kinds
    assert "cve" in kinds


# ---------- 範囲外 index の落下 ----------


def test_fact_with_out_of_range_or_unset_index_is_dropped() -> None:
    member = _member("m1", body="通常の記述。CVE の言及なし。")
    draft = EventNewsDraft(
        headline="h",
        bluf="b",
        facts=[
            FactItem(text="正常な事実", source_index=1),
            FactItem(text="範囲外の参照", source_index=5),
            FactItem(text="未指定の参照", source_index=0),
        ],
    )

    result = verify_draft(draft, (member,))

    assert len(result.draft.facts) == 1
    assert result.draft.facts[0].text == "正常な事実"
    assert result.dropped_lines == 2


# ---------- cross-member 転植 ----------


def test_cross_member_transplant_is_repaired_or_substituted() -> None:
    m1 = _member("m1", body="CVE-2024-1111 の悪用が確認された。")
    m2 = _member("m2", body="別製品の CVE-2024-2222 が影響を受ける。")
    draft = EventNewsDraft(
        headline="h",
        bluf="b",
        # 記事 1 の CVE を、記事 2 (source_index=2) 参照の行に誤って書いた。
        facts=[FactItem(text="影響を受けるのは CVE-2024-1111 だ。", source_index=2)],
    )

    result = verify_draft(draft, (m1, m2))

    assert len(result.draft.facts) == 1
    text = result.draft.facts[0].text
    assert "CVE-2024-1111" not in text
    assert result.repaired_ids + result.substituted_ids == 1


def test_cross_member_transplant_repairs_to_correct_id_when_unique() -> None:
    m1 = _member("m1", body="CVE-2024-1111 の悪用が確認された。")
    m2 = _member("m2", body="別製品の CVE-2024-2222 のみが影響を受ける。")
    draft = EventNewsDraft(
        headline="h",
        bluf="b",
        facts=[FactItem(text="影響を受けるのは CVE-2024-1111 だ。", source_index=2)],
    )

    result = verify_draft(draft, (m1, m2))

    assert result.repaired_ids == 1
    assert result.substituted_ids == 0
    assert "CVE-2024-2222" in result.draft.facts[0].text


def test_unrepairable_identifier_is_substituted_but_line_kept() -> None:
    m1 = _member("m1", body="CVE-2024-1111 の悪用が確認された。")
    # 記事 2 には CVE が 2 件あり、一意な repair 先を決められない (曖昧)。
    m2 = _member("m2", body="CVE-2024-3333 と CVE-2024-4444 の両方に言及。")
    draft = EventNewsDraft(
        headline="h",
        bluf="b",
        facts=[FactItem(text="影響を受けるのは CVE-2024-1111 だ。", source_index=2)],
    )

    result = verify_draft(draft, (m1, m2))

    assert len(result.draft.facts) == 1
    assert "(原文参照)" in result.draft.facts[0].text
    assert result.substituted_ids == 1
    assert result.repaired_ids == 0


def test_matching_identifier_is_left_unchanged() -> None:
    m1 = _member("m1", body="CVE-2024-1111 の悪用が確認された。")
    draft = EventNewsDraft(
        headline="h",
        bluf="b",
        facts=[FactItem(text="影響を受けるのは CVE-2024-1111 だ。", source_index=1)],
    )

    result = verify_draft(draft, (m1,))

    assert result.draft.facts[0].text == "影響を受けるのは CVE-2024-1111 だ。"
    assert result.repaired_ids == 0
    assert result.substituted_ids == 0
    assert result.verified is True


# ---------- 本文空 (purge 済み) で保持 ----------


def test_empty_body_line_is_preserved_and_marks_unverified() -> None:
    # purge 済み = title 以外の照合材料が無い (summary も空)。識別子は保持し verified=False
    member = _member("m1", summary="", body="")
    draft = EventNewsDraft(
        headline="h",
        bluf="b",
        facts=[FactItem(text="CVE-2024-1111 が使われた。", source_index=1)],
    )

    result = verify_draft(draft, (member,))

    assert len(result.draft.facts) == 1
    assert result.draft.facts[0].text == "CVE-2024-1111 が使われた。"
    assert result.verified is False
    assert result.repaired_ids == 0
    assert result.substituted_ids == 0


def test_lines_without_identifiers_do_not_affect_verified_flag() -> None:
    member = _member("m1", summary="", body="")
    draft = EventNewsDraft(
        headline="h",
        bluf="b",
        facts=[FactItem(text="識別子を含まない事実。", source_index=1)],
    )

    result = verify_draft(draft, (member,))

    assert result.draft.facts[0].text == "識別子を含まない事実。"
    assert result.verified is True


# ---------- discrepancies は source_index=0 でも残る ----------


def test_discrepancy_with_index_zero_is_kept() -> None:
    m1 = _member("m1", body="CVE-2024-1111 が確認された。")
    m2 = _member("m2", body="別の情報源はこの脆弱性に言及していない。")
    draft = EventNewsDraft(
        headline="h",
        bluf="b",
        discrepancies=[FactItem(text="初期侵入経路はどの媒体も特定していない。", source_index=0)],
    )

    result = verify_draft(draft, (m1, m2))

    assert len(result.draft.discrepancies) == 1
    assert result.draft.discrepancies[0].text == "初期侵入経路はどの媒体も特定していない。"


def test_discrepancy_with_index_zero_uses_union_matching() -> None:
    m1 = _member("m1", body="CVE-2024-1111 が確認された。")
    m2 = _member("m2", body="別の情報源はこの脆弱性に言及していない。")
    draft = EventNewsDraft(
        headline="h",
        bluf="b",
        discrepancies=[FactItem(text="CVE-2024-1111 の詳細は媒体間で一致した。", source_index=0)],
    )

    result = verify_draft(draft, (m1, m2))

    # union (m1+m2) に CVE-2024-1111 が実在するため、置換・repair は不要。
    assert result.draft.discrepancies[0].text == "CVE-2024-1111 の詳細は媒体間で一致した。"
    assert result.repaired_ids == 0
    assert result.substituted_ids == 0
