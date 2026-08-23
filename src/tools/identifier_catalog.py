"""識別子カタログ — LLM に識別子を「書かせない」ための番号参照機構。

**設計の根拠 (2026-08-23)**: 従来の関門は「生成文から regex で抽出 → 原文を部分文字列 +
語境界で検索」という **非対称な 2 経路**で判定していた。両経路がズレる箇所すべてが
バグになり、実際 1 日で 5 種の誤判定が出た (IOC regex が破損値を検査対象外にする /
``CVSS 10.0`` を ``0.0`` と切る / 4 部版数を IPv4 と誤認 / IP を version と二重計上 /
``isalnum()`` が CJK を語中文字と判定)。識別子の型 × 表記 × 言語の組合せは開いており、
個別修正では収束しない。

構造で閉じる:

1. **番号参照** — プロンプトには ``I1 = CVE-2026-18051`` の形式でカタログを提示し、
   本文には ``{I1}`` と書かせる。実値を書かせないので **転記誤りが発生し得ない**
   (2026-08-22 の引用関門で確立した「長い id を写させず番号で参照させる」と同型)。
2. **対称照合** — やむを得ず実値が書かれた場合の検査は、**原文にも同じ抽出器をかけて
   作った集合への帰属**で行う。判定経路が 1 本になるため、抽出器の癖は両側で相殺され
   誤検出にならない (抽出漏れは「検査しない」に倒れるだけで、本文を壊さない)。
3. **厳密文法のみを強制対象**にする — CVE/IP/domain/hash/actor_id は文法が厳密で誤りの
   実害が直接的 (誤った IP を遮断する等) なので置換する。version/cvss は表記の変種が
   無限で誤りの害も小さいため、**本文を書き換えず計数のみ**行う。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.tools.identifier_match import Identifier, extract_identifiers

# 置換 (本文の書き換え) まで行う厳密文法の型。それ以外は計数のみ。
STRICT_KINDS: frozenset[str] = frozenset({"cve", "ip", "domain", "hash", "actor_id"})

# 解決できなかった識別子の代替表記 (行は残す — 観測の事実は保持する)
UNRESOLVED_PLACEHOLDER = "(原文参照)"

# LLM が書く参照の許容形: {I3} / ｛I3｝ / [I3] / I3 (単独トークン)
_REF_RE = re.compile(r"[{｛\[]\s*(I\d{1,3})\s*[}｝\]]|(?<![A-Za-z0-9])(I\d{1,3})(?![A-Za-z0-9])")

# 参照として解決されなかった中括弧 (装飾用途 / 捏造番号)。中身は残す。
_BRACE_RE = re.compile(r"[{｛]\s*([^{}｛｝]{1,60}?)\s*[}｝]")


@dataclass(frozen=True)
class CatalogEntry:
    """カタログ 1 件。``token`` は ``I1`` 形式、``members`` は 1-based の記事番号集合。"""

    token: str
    identifier: Identifier
    members: frozenset[int]


@dataclass(frozen=True)
class IdentifierCatalog:
    entries: tuple[CatalogEntry, ...]

    @property
    def by_token(self) -> Mapping[str, CatalogEntry]:
        return {e.token: e for e in self.entries}

    @property
    def normalized_values(self) -> frozenset[str]:
        return frozenset(e.identifier.normalized for e in self.entries)

    def members_of(self, normalized: str) -> frozenset[int]:
        for e in self.entries:
            if e.identifier.normalized == normalized:
                return e.members
        return frozenset()


@dataclass(frozen=True)
class ResolveStats:
    """1 draft ぶんの解決結果。すべて版に永続化して常設監視する (関門は黙って落とす)。"""

    resolved: int = 0  # {In} → 実値に解決した数
    unknown_refs: int = 0  # カタログに無い {In} (creation) — 表記を除去
    misattributed: int = 0  # 参照先記事に無い識別子を引いた数 (実値は保持・計数のみ)
    literal_ok: int = 0  # 実値で書かれ、カタログに在った数
    unwrapped: int = 0  # 参照でない中括弧を外した数 (LLM が略語を装飾に使う癖)
    literal_substituted: int = 0  # 実値で書かれ、厳密型でカタログに無い → 置換した数
    literal_flagged: int = 0  # 実値で書かれ、緩い型でカタログに無い → 計数のみ

    def merged(self, other: ResolveStats) -> ResolveStats:
        return ResolveStats(
            resolved=self.resolved + other.resolved,
            unknown_refs=self.unknown_refs + other.unknown_refs,
            misattributed=self.misattributed + other.misattributed,
            literal_ok=self.literal_ok + other.literal_ok,
            literal_substituted=self.literal_substituted + other.literal_substituted,
            literal_flagged=self.literal_flagged + other.literal_flagged,
            unwrapped=self.unwrapped + other.unwrapped,
        )


def build_catalog(member_texts: Sequence[str]) -> IdentifierCatalog:
    """記事ごとのテキストから識別子カタログを作る (原文側の抽出は 1 経路のみ)。

    同一の識別子が複数記事に出る場合は 1 エントリにまとめ、``members`` に全記事番号を持つ。
    順序は「登場記事の若い順 → 型 → 値」で決定論。
    """
    found: dict[str, tuple[Identifier, set[int]]] = {}
    for idx, text in enumerate(member_texts, start=1):
        for ident in extract_identifiers(text):
            key = ident.normalized
            if key in found:
                found[key][1].add(idx)
            else:
                found[key] = (ident, {idx})
    ordered = sorted(found.values(), key=lambda p: (min(p[1]), p[0].kind, p[0].normalized))
    return IdentifierCatalog(
        entries=tuple(
            CatalogEntry(token=f"I{i}", identifier=ident, members=frozenset(members))
            for i, (ident, members) in enumerate(ordered, start=1)
        )
    )


def render_catalog(catalog: IdentifierCatalog) -> str:
    """プロンプトへ載せる一覧。実値はここにだけ書かれ、本文には番号で参照させる。"""
    if not catalog.entries:
        return "(この事象に識別子はありません。本文にも識別子を書かないこと)"
    lines = [
        f"{e.token} = {e.identifier.raw}  ({e.identifier.kind}, 記事 "
        + "".join(f"[{m}]" for m in sorted(e.members))
        + ")"
        for e in catalog.entries
    ]
    return "\n".join(lines)


def resolve_text(
    text: str,
    catalog: IdentifierCatalog,
    *,
    cited_member: int = 0,
) -> tuple[str, ResolveStats]:
    """本文中の ``{In}`` を実値へ解決し、実値直書きを対称照合で検査する。

    ``cited_member`` が 1 以上なら、その記事に存在しない識別子の引用を ``misattributed``
    として数える (別記事の CVE を別製品に付ける cross-member 転植の検出)。0 は
    「特定記事に紐づかない行」(headline/bluf/不在の主張) で、帰属検査を行わない。
    """
    by_token = catalog.by_token
    stats = ResolveStats()

    def _sub(m: re.Match[str]) -> str:
        nonlocal stats
        token = m.group(1) or m.group(2)
        entry = by_token.get(token)
        if entry is None:
            stats = stats.merged(ResolveStats(unknown_refs=1))
            # カタログに無い番号は創作。**無言で削除しない** — 削ると主語が消えて
            # 「は、…」で始まる壊れた文が残る (2026-08-23 実測 2 行)。印を残して
            # 観測の事実を保持し、落ちたことを読み手に見せる (引用関門と同じ思想)。
            return UNRESOLVED_PLACEHOLDER
        mis = 1 if cited_member and cited_member not in entry.members else 0
        stats = stats.merged(ResolveStats(resolved=1, misattributed=mis))
        return entry.identifier.raw

    # 実値直書きの検査は **参照を除いた本文**に対して行う (解決済みの値を
    # 二重計数しないため)。照合は対称 — 生成側もカタログ側も extract_identifiers。
    # 参照でない中括弧は **外して中身を残す** (2026-08-23 実測)。LLM は `{…}` を
    # 「技術用語を囲む記法」と解釈し `{VPN}` `{HTTP}` `{PDF}` のような略語まで囲む。
    # さらに `{GT42}` `{OAuth68}` のように **語+数字で偽の参照番号を捏造**する
    # (`I`+数字 の書式を真似る)。中身を残せば読める文になり、実値であれば下の
    # 対称照合が拾う。番号参照の書式そのものを変える案もあるが、原文引用の [N] と
    # 衝突しない記号が乏しく、まず「外す」で観測する。
    stripped = _REF_RE.sub(" ", text)
    literal_source = _BRACE_RE.sub(lambda m: m.group(1), stripped)
    known = catalog.normalized_values
    substitutions: list[str] = []
    for ident in extract_identifiers(literal_source):
        if ident.normalized in known:
            mis = (
                1
                if cited_member and cited_member not in catalog.members_of(ident.normalized)
                else 0
            )
            stats = stats.merged(ResolveStats(literal_ok=1, misattributed=mis))
            continue
        if ident.kind in STRICT_KINDS:
            substitutions.append(ident.raw)
            stats = stats.merged(ResolveStats(literal_substituted=1))
        else:
            # version/cvss 等は表記の変種が無限。本文は壊さず計数のみ (§C)
            stats = stats.merged(ResolveStats(literal_flagged=1))

    out = _REF_RE.sub(_sub, text)
    leftover = len(_BRACE_RE.findall(out))
    if leftover:
        out = _BRACE_RE.sub(lambda m: m.group(1), out)
        stats = stats.merged(ResolveStats(unwrapped=leftover))
    for raw in substitutions:
        out = out.replace(raw, UNRESOLVED_PLACEHOLDER)
    return re.sub(r"\s{2,}", " ", out).strip(), stats
