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

from collections.abc import Sequence

from src.eventnews.models import EventNewsDraft, FactItem, GateResult, MemberArticle
from src.tools.identifier_catalog import (
    IdentifierCatalog,
    ResolveStats,
    build_catalog,
    render_catalog,
    resolve_text,
)


def member_text(member: MemberArticle) -> str:
    """カタログ構築・照合に使う 1 記事分のテキスト (本文が空でも title/summary は使う)。"""
    return f"{member.title}\n{member.summary}\n{member.body}"


def build_member_catalog(members: Sequence[MemberArticle]) -> IdentifierCatalog:
    return build_catalog([member_text(m) for m in members])


def render_allowed_identifiers(members: Sequence[MemberArticle]) -> str:
    """プロンプトへ載せるカタログ文字列 (実値はここにだけ現れる)。"""
    return render_catalog(build_member_catalog(members))


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
        total = total.merged(st)
        facts.append(FactItem(text=text, source_index=item.source_index))

    discrepancies: list[FactItem] = []
    for item in draft.discrepancies:
        cited = item.source_index if 1 <= item.source_index <= n_members else 0
        text, st = resolve_text(item.text, catalog, cited_member=cited)
        total = total.merged(st)
        discrepancies.append(FactItem(text=text, source_index=item.source_index))

    unknowns: list[str] = []
    for raw in draft.unknowns:
        text, st = resolve_text(raw, catalog, cited_member=0)
        total = total.merged(st)
        unknowns.append(text)

    headline, st_head = resolve_text(draft.headline, catalog, cited_member=0)
    total = total.merged(st_head)
    bluf, st_bluf = resolve_text(draft.bluf, catalog, cited_member=0)
    total = total.merged(st_bluf)

    return GateResult(
        draft=EventNewsDraft(
            headline=headline, bluf=bluf, facts=facts,
            discrepancies=discrepancies, unknowns=unknowns,
        ),
        dropped_lines=dropped,
        repaired_ids=total.resolved,
        substituted_ids=total.literal_substituted + total.unknown_refs,
        verified=bool(catalog.entries) or not any(
            (total.literal_flagged, total.literal_substituted, total.unknown_refs)
        ),
        stats=total,
    )
