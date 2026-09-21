"""識別子関門 — カタログの番号参照を実値へ解決し、直書きを対称照合で検査する。

**2026-08-23 の抜本改修**: 旧実装は「生成文から抽出 → 原文を部分文字列 + 語境界で検索」
という非対称な 2 経路で判定しており、両者がズレる箇所すべてがバグになっていた
(1 日で 5 種の誤判定)。識別子の型 × 表記 × 言語の組合せは開いており、個別修正では
収束しない。よって:

- **識別子は LLM に書かせない** — プロンプトはカタログ (``I1 = CVE-…``) を提示し、
  本文には ``{I1}`` と書かせる。転記誤りが検出対象でなく **発生し得ない**ものになる
  (2026-08-22 の引用関門で確立した番号参照と同型)
- **検査は対称** — やむを得ず実値が書かれた場合のみ、原文にも同じ抽出器をかけて作った
  集合への帰属で判定する。判定経路が 1 本なので抽出器の癖は両側で相殺される
- **強制は厳密文法のみ** — CVE/IP/domain/hash/actor_id は置換、version/cvss は計数のみ

[N] 関門 (source_index の範囲検査と行落とし) は facts のみに適用する — 不在の主張
(「どの媒体も特定していない」) は原理的に [N] を持てないため。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from src.eventnews.models import EventNewsDraft, FactItem, GateResult, MemberArticle
from src.tools.identifier_catalog import (
    IdentifierCatalog,
    ResolveStats,
    build_catalog,
    render_catalog,
    resolve_text,
)
from src.tools.identifier_match import extract_identifiers

# 本文末尾に LLM が書いた出典番号。表示側が source_index から付けるため二重になる
# (実測 532 行中 155 行 = 29%)。プロンプトでも禁止したが、**指示は関門にならない**ので
# 決定論でも落とす (2026-08-19 の規約)。行中の [N] は文意に関わるため末尾のみ除去。
_TRAILING_CITE_RE = re.compile(r"(?:\s*\[\d{1,2}\])+\s*([。.!?！？])?\s*$")


def strip_trailing_citation(text: str) -> str:
    """行末の [N] を落とす (句点が前後どちらにあっても文末の句点は保つ)。"""
    return _TRAILING_CITE_RE.sub(lambda m: m.group(1) or "", text.rstrip())


def member_text(member: MemberArticle) -> str:
    """カタログ構築・照合に使う 1 記事分のテキスト (本文が空でも title/summary は使う)。"""
    return f"{member.title}\n{member.summary}\n{member.body}"


def build_member_catalog(members: Sequence[MemberArticle]) -> IdentifierCatalog:
    return build_catalog([member_text(m) for m in members])


#: プロンプトに提示する識別子の上限。IOC 一括列挙の記事で 1,000 件超になり、プロンプトが
#: 60k tok に膨らんだ (2026-09-21)。1 件 ≈ 60 字なので 120 件で ≈ 7k 字。重要な型
#: (cve / version / cvss) は優先して残る (``identifier_catalog._RENDER_PRIORITY``)。
CATALOG_PROMPT_MAX_ENTRIES = 120


def render_allowed_identifiers(
    members: Sequence[MemberArticle], *, max_entries: int | None = CATALOG_PROMPT_MAX_ENTRIES
) -> str:
    """プロンプトへ載せるカタログ文字列 (実値はここにだけ現れる)。上限つき。"""
    return render_catalog(build_member_catalog(members), max_entries=max_entries)


#: 「本文に入っているべき」種類の識別子。ドメイン・ハッシュ・IP は一括列挙の
#: 記事で数十〜数百個になり、全部入れると記事が壊れるので対象にしない。
IMPORTANT_KINDS: frozenset[str] = frozenset({"cve", "version", "cvss"})


def missing_important_identifiers(
    draft: EventNewsDraft, catalog: IdentifierCatalog
) -> tuple[tuple[str, str, str], ...]:
    """カタログの重要識別子のうち、生成物のどこにも現れないもの (token, 実値, 種類)。

    31B の実測 (2026-08-27): カタログを見せられた上で重要識別子の半分しか使わない。
    散文の一般指示 (「具体値を落とすな」) は 3 度無効だったが、**具体的な番号の指摘**
    (「{I3} (version: 1.8.6) が無い」) による書き直しは不足 5 → 0 に埋めた。
    caveats 欄と同じ「構造は効く」側の介入。
    """
    text = (
        draft.bluf
        + " ".join(f.text for f in draft.facts)
        + " ".join(c.text for c in draft.caveats)
        + " ".join(draft.unknowns)
    )
    used = {i.normalized for i in extract_identifiers(text)}
    found: list[tuple[str, str, str]] = []
    for entry in catalog.entries:
        if not entry.token or entry.identifier.kind not in IMPORTANT_KINDS:
            continue
        if entry.identifier.normalized not in used:
            found.append((entry.token, entry.identifier.raw, entry.identifier.kind))
    return tuple(found)


def verify_draft(draft: EventNewsDraft, members: Sequence[MemberArticle]) -> GateResult:
    """structured 出力の識別子解決 + [N] 関門を通す。"""
    catalog = build_member_catalog(members)
    total = ResolveStats()
    n_members = len(members)

    facts: list[FactItem] = []
    dropped = 0
    for item in draft.facts:
        if not (1 <= item.source_index <= n_members):
            dropped += 1  # 0 / 範囲外 / 創作番号は行ごと落とす
            continue
        text, st = resolve_text(item.text, catalog, cited_member=item.source_index)
        text = strip_trailing_citation(text)
        total = total.merged(st)
        # ⚠ **フィールドを 1 つでも渡し忘れると既定値に落ちる**。2026-08-26 まで
        # section を渡しておらず、本番 542 版の全 fact が既定の "what" に潰れていた
        # (生成側は節を振れていたのに、保存された時点で失われていた)。
        facts.append(
            FactItem(
                text=text,
                source_index=item.source_index,
                paragraph=item.paragraph,
                section=item.section,
            )
        )

    discrepancies: list[FactItem] = []
    for item in draft.discrepancies:
        cited = item.source_index if 1 <= item.source_index <= n_members else 0
        text, st = resolve_text(item.text, catalog, cited_member=cited)
        total = total.merged(st)
        discrepancies.append(
            FactItem(
                text=text,
                source_index=item.source_index,
                paragraph=item.paragraph,
                section=item.section,
            )
        )

    unknowns: list[str] = []
    for raw in draft.unknowns:
        text, st = resolve_text(raw, catalog, cited_member=0)
        total = total.merged(st)
        unknowns.append(text)

    caveats: list[FactItem] = []
    for item in draft.caveats:
        cited = item.source_index if 1 <= item.source_index <= n_members else 0
        text, st = resolve_text(item.text, catalog, cited_member=cited)
        total = total.merged(st)
        caveats.append(
            FactItem(
                text=text,
                source_index=item.source_index,
                paragraph=item.paragraph,
                section=item.section,
            )
        )

    key_points: list[str] = []
    for raw in draft.key_points:
        text, st = resolve_text(raw, catalog, cited_member=0)
        total = total.merged(st)
        key_points.append(text)

    headline, st_head = resolve_text(draft.headline, catalog, cited_member=0)
    total = total.merged(st_head)
    bluf, st_bluf = resolve_text(draft.bluf, catalog, cited_member=0)
    total = total.merged(st_bluf)

    return GateResult(
        draft=EventNewsDraft(
            headline=headline,
            bluf=bluf,
            key_points=key_points,
            facts=facts,
            caveats=caveats,
            discrepancies=discrepancies,
            unknowns=unknowns,
        ),
        dropped_lines=dropped,
        repaired_ids=total.resolved,
        substituted_ids=total.literal_substituted + total.unknown_refs,
        verified=bool(catalog.entries)
        or not any((total.literal_flagged, total.literal_substituted, total.unknown_refs)),
        stats=total,
    )
