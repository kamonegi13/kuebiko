"""事象単位ニュースの識別子関門 (docs/event_news_design.md §9)。

生成された structured 出力の識別子を、**source_index が指す 1 記事のみ**に対して
照合する (全メンバー和集合との照合は cross-member 転植 — 別記事の CVE が別製品に
付く — を素通しするため禁止。レビュー B C1)。discrepancies の source_index=0
(「どの媒体も特定していない」等の不在の主張) と unknowns だけは和集合照合を使う。
"""

from __future__ import annotations

from collections.abc import Sequence

from src.cti.ioc_extractor import refang
from src.eventnews.models import EventNewsDraft, FactItem, GateResult, MemberArticle
from src.tools.identifier_match import (
    Identifier,
    contains_identifier,
    extract_identifiers,
    find_repair_candidate,
)

# 解決不能な識別子の置換先 (原文が特定できないことを明示する)
_UNRESOLVED_PLACEHOLDER = "(原文参照)"


def _member_text(member: MemberArticle) -> str:
    return f"{member.title}\n{member.summary}\n{member.body}"


def _union_text(members: Sequence[MemberArticle]) -> str:
    return "\n".join(_member_text(m) for m in members)


def extract_allowed_by_member(
    members: Sequence[MemberArticle],
) -> tuple[tuple[Identifier, ...], ...]:
    """メンバーごと (title+summary+body) の「使ってよい識別子」一覧を返す。

    プロンプトへ渡す整形は担当外 (identifier 一覧を返すだけ)。
    """
    return tuple(extract_identifiers(_member_text(m)) for m in members)


def _verify_line(
    text: str,
    haystack: str,
    allowed: tuple[Identifier, ...],
    *,
    verifiable: bool,
) -> tuple[str, int, int, bool]:
    """1 行の識別子を照合し、置換後テキストと集計を返す。

    戻り値: (置換後テキスト, repaired 数, substituted 数, その行を検証できたか)。
    ``text`` はまず ``refang`` し、以降の識別子抽出・置換をすべてこの正規化済み
    テキストに対して行う (``extract_identifiers`` 内部の refang と揃え、
    抽出した ``raw`` が必ず置換対象のテキスト中に literal に存在するようにする)。
    """
    canonical_text = refang(text)
    found = extract_identifiers(canonical_text)
    if not found:
        return canonical_text, 0, 0, True
    if not verifiable:
        # 本文 purge 済み等で照合不能: 検証不能 ≠ 反証。行はそのまま保持する。
        return canonical_text, 0, 0, False

    new_text = canonical_text
    repaired = 0
    substituted = 0
    for ident in found:
        if contains_identifier(haystack, ident):
            continue
        candidate = find_repair_candidate(ident, allowed)
        if candidate is not None:
            new_text = new_text.replace(ident.raw, candidate.raw)
            repaired += 1
        else:
            new_text = new_text.replace(ident.raw, _UNRESOLVED_PLACEHOLDER)
            substituted += 1
    return new_text, repaired, substituted, True


def _process_facts(
    facts: Sequence[FactItem],
    members: Sequence[MemberArticle],
    allowed_by_member: Sequence[tuple[Identifier, ...]],
) -> tuple[list[FactItem], int, int, int, bool]:
    """facts の [N] 関門 + 識別子関門。範囲外/未指定 (source_index) は行ごと落とす。"""
    kept: list[FactItem] = []
    dropped_lines = 0
    repaired_total = 0
    substituted_total = 0
    verified = True

    for fact in facts:
        idx = fact.source_index
        if idx < 1 or idx > len(members):
            dropped_lines += 1
            continue

        member = members[idx - 1]
        new_text, repaired, substituted, line_verified = _verify_line(
            fact.text,
            _member_text(member),
            allowed_by_member[idx - 1],
            verifiable=bool(member.summary or member.body),
        )
        repaired_total += repaired
        substituted_total += substituted
        verified = verified and line_verified
        kept.append(FactItem(text=new_text, source_index=idx))

    return kept, dropped_lines, repaired_total, substituted_total, verified


def _process_discrepancies(
    items: Sequence[FactItem],
    members: Sequence[MemberArticle],
    allowed_by_member: Sequence[tuple[Identifier, ...]],
    union_allowed: tuple[Identifier, ...],
) -> tuple[list[FactItem], int, int, bool]:
    """discrepancies は [N] を要求しない。source_index=0/範囲外は和集合照合で残す。"""
    kept: list[FactItem] = []
    repaired_total = 0
    substituted_total = 0
    verified = True
    union_text = _union_text(members)
    union_verifiable = any(bool(m.summary or m.body) for m in members)

    for item in items:
        idx = item.source_index
        if 1 <= idx <= len(members):
            member = members[idx - 1]
            haystack = _member_text(member)
            allowed = allowed_by_member[idx - 1]
            verifiable = bool(member.summary or member.body)
        else:
            haystack = union_text
            allowed = union_allowed
            verifiable = union_verifiable

        new_text, repaired, substituted, line_verified = _verify_line(
            item.text, haystack, allowed, verifiable=verifiable
        )
        repaired_total += repaired
        substituted_total += substituted
        verified = verified and line_verified
        kept.append(FactItem(text=new_text, source_index=idx))

    return kept, repaired_total, substituted_total, verified


def _process_unknowns(
    items: Sequence[str],
    members: Sequence[MemberArticle],
    union_allowed: tuple[Identifier, ...],
) -> tuple[list[str], int, int, bool]:
    """unknowns の識別子は和集合照合 (どの記事にも書かれていない、が主張の本質)。"""
    haystack = _union_text(members)
    verifiable = any(bool(m.summary or m.body) for m in members)
    kept: list[str] = []
    repaired_total = 0
    substituted_total = 0
    verified = True

    for text in items:
        new_text, repaired, substituted, line_verified = _verify_line(
            text, haystack, union_allowed, verifiable=verifiable
        )
        repaired_total += repaired
        substituted_total += substituted
        verified = verified and line_verified
        kept.append(new_text)

    return kept, repaired_total, substituted_total, verified


def verify_draft(draft: EventNewsDraft, members: Sequence[MemberArticle]) -> GateResult:
    """structured 出力の識別子関門 (2 段) + [N] 関門 (facts のみ) を通す。"""
    allowed_by_member = extract_allowed_by_member(members)
    union_allowed = tuple(ident for group in allowed_by_member for ident in group)

    facts, dropped_lines, facts_repaired, facts_substituted, facts_verified = _process_facts(
        draft.facts, members, allowed_by_member
    )
    discrepancies, disc_repaired, disc_substituted, disc_verified = _process_discrepancies(
        draft.discrepancies, members, allowed_by_member, union_allowed
    )
    unknowns, unk_repaired, unk_substituted, unk_verified = _process_unknowns(
        draft.unknowns, members, union_allowed
    )

    new_draft = EventNewsDraft(
        headline=draft.headline,
        bluf=draft.bluf,
        facts=facts,
        discrepancies=discrepancies,
        unknowns=unknowns,
    )
    return GateResult(
        draft=new_draft,
        dropped_lines=dropped_lines,
        repaired_ids=facts_repaired + disc_repaired + unk_repaired,
        substituted_ids=facts_substituted + disc_substituted + unk_substituted,
        verified=facts_verified and disc_verified and unk_verified,
    )
