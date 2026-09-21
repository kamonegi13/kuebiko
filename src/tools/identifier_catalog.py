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
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from src.tools.identifier_match import Identifier, extract_identifiers

# 置換 (本文の書き換え) まで行う厳密文法の型。それ以外は計数のみ。
STRICT_KINDS: frozenset[str] = frozenset({"cve", "ip", "domain", "hash", "actor_id"})

# カタログ (= プロンプトに実値を提示する一覧) に載せない型。
# 固有名詞を載せたところ、26B が「使ってよい識別子」を **企業名リストと誤解して羅列**
# した (McDonald、TCS、BEC、AI、CEO、PDF、SECURITY… 2026-08-23 実測)。
# 検出 (直書きの照合・破損の計数) には引き続き使う — 載せないのは提示だけ。
CATALOG_EXCLUDED_KINDS: frozenset[str] = frozenset({"proper_noun"})

# 取り違え (near-miss) の判定閾値 — **実測から置く** (2026-09-19)。
# カタログに無い version/cvss は 3 腕 116 件で 58 件あったが、その大半は 7.5 / 9.1 のような
# 短い値で、一律に置換すると**正しい記述を壊す** (§C の「計数のみ」判断はこれが根拠だった)。
# 長い値が 1 文字だけ違うときに限れば、同じ標本で発火は実例 1 件のみ
# (``151.0.79222.138`` ← ``151.0.7922.138`` の 1 桁挿入) で誤検出なし。
NEAR_MISS_MIN_LEN = 6
NEAR_MISS_MAX_DISTANCE = 1


def _edit_distance(a: str, b: str, *, cap: int) -> int:
    """挿入/削除/置換の編集距離。``cap`` を超えるものは打ち切って ``cap + 1`` を返す。"""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def near_miss_value(written: str, known: Iterable[str]) -> str | None:
    """``written`` が取り違えなら、意図されたカタログ値を返す (純粋関数)。

    取り違えと見なす条件は 3 つすべて:
    長さが ``NEAR_MISS_MIN_LEN`` 以上 / 編集距離が ``NEAR_MISS_MAX_DISTANCE`` 以内 /
    **その距離の候補がちょうど 1 つ**。候補が複数あれば意図を決められないので直さない。
    """
    if len(written) < NEAR_MISS_MIN_LEN:
        return None
    hits = [
        k
        for k in known
        if k
        and k != written
        and _edit_distance(written, k, cap=NEAR_MISS_MAX_DISTANCE) <= NEAR_MISS_MAX_DISTANCE
    ]
    return hits[0] if len(hits) == 1 else None


# 解決できなかった識別子の代替表記 (行は残す — 観測の事実は保持する)
UNRESOLVED_PLACEHOLDER = "(原文参照)"

# LLM が書く参照の許容形: {I3} / ｛I3｝ / [I3] / I3 (単独トークン)
_REF_RE = re.compile(r"[{｛\[]\s*(I\d{1,3})\s*[}｝\]]|(?<![A-Za-z0-9])(I\d{1,3})(?![A-Za-z0-9])")

# 想定される字種 (日本語 CTI ブリーフに出てよいもの)。ここから外れる文字は
# **生成の破損**の兆候 (実測: `2026年` が `202막年` になりハングルが混入)。
# 識別子と違い文法が閉じているので検査が確実。本文は壊さず計数のみ。
_EXPECTED_SCRIPT_PREFIXES = (
    "LATIN",
    "HIRAGANA",
    "KATAKANA",
    "CJK UNIFIED",
    "FULLWIDTH",
    "HALFWIDTH",
    "IDEOGRAPHIC",
    "DIGIT",
    "GREEK",
    "CYRILLIC",
)


def count_script_anomalies(text: str) -> int:
    """想定外の字種の文字数を数える (ASCII・記号・約物は対象外)。"""
    n = 0
    for ch in text:
        if ch.isascii() or ch.isspace():
            continue
        category = unicodedata.category(ch)
        if category.startswith(("P", "S", "Z")):
            continue  # 約物・記号 (、。「」※ ™ 等)
        name = unicodedata.name(ch, "")
        if not name.startswith(_EXPECTED_SCRIPT_PREFIXES):
            n += 1
    return n


# 参照として解決されなかった中括弧 (装飾用途 / 捏造番号)。中身は残す。
_BRACE_RE = re.compile(r"[{｛]\s*([^{}｛｝]{1,60}?)\s*[}｝]")


@dataclass(frozen=True)
class CatalogEntry:
    """カタログ 1 件。

    ``token`` は ``I1`` 形式。``CATALOG_EXCLUDED_KINDS`` の型は**空文字**で、
    プロンプトに提示されない (照合・計数には使う)。番号は提示するものだけに
    連番で振る — 穴が空くと LLM が存在しない番号を推測する。
    """

    token: str
    identifier: Identifier
    members: frozenset[int]


@dataclass(frozen=True)
class IdentifierCatalog:
    entries: tuple[CatalogEntry, ...]

    @property
    def by_token(self) -> Mapping[str, CatalogEntry]:
        return {e.token: e for e in self.entries if e.token}

    @property
    def normalized_values(self) -> frozenset[str]:
        return frozenset(e.identifier.normalized for e in self.entries)

    def raw_of(self, normalized: str) -> str | None:
        """正規化値から原文の表記を引く (取り違えを直すとき、原文の見た目で戻す)。"""
        for e in self.entries:
            if e.identifier.normalized == normalized:
                return e.identifier.raw
        return None

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
    script_anomalies: int = 0  # 想定外の字種 (生成破損の兆候。本文は壊さない)
    literal_substituted: int = 0  # 実値で書かれ、厳密型でカタログに無い → 置換した数
    literal_flagged: int = 0  # 実値で書かれ、緩い型でカタログに無い → 計数のみ
    literal_repaired: int = 0  # 取り違え (長い値の 1 文字違い) をカタログ値へ直した数

    def merged(self, other: ResolveStats) -> ResolveStats:
        return ResolveStats(
            resolved=self.resolved + other.resolved,
            unknown_refs=self.unknown_refs + other.unknown_refs,
            misattributed=self.misattributed + other.misattributed,
            literal_ok=self.literal_ok + other.literal_ok,
            literal_substituted=self.literal_substituted + other.literal_substituted,
            literal_flagged=self.literal_flagged + other.literal_flagged,
            literal_repaired=self.literal_repaired + other.literal_repaired,
            unwrapped=self.unwrapped + other.unwrapped,
            script_anomalies=self.script_anomalies + other.script_anomalies,
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
    entries: list[CatalogEntry] = []
    n = 0
    for ident, members in ordered:
        if ident.kind in CATALOG_EXCLUDED_KINDS:
            entries.append(CatalogEntry(token="", identifier=ident, members=frozenset(members)))
            continue
        n += 1
        entries.append(CatalogEntry(token=f"I{n}", identifier=ident, members=frozenset(members)))
    return IdentifierCatalog(entries=tuple(entries))


#: 提示上限に当たったとき残す優先順 (重要 → 固有 → 一括列挙されがちな IOC)。
#: 未掲載の型はこの後ろ。⚠ 順位は「本文に入っているべき度合い」で、
#: ``identifier_gate.IMPORTANT_KINDS`` (cve / version / cvss) が必ず先頭に来る。
_RENDER_PRIORITY: tuple[str, ...] = ("cve", "version", "cvss", "actor_id", "domain", "ip", "hash")


def _render_rank(kind: str) -> int:
    return _RENDER_PRIORITY.index(kind) if kind in _RENDER_PRIORITY else len(_RENDER_PRIORITY)


def render_catalog(catalog: IdentifierCatalog, *, max_entries: int | None = None) -> str:
    """プロンプトへ載せる一覧。実値はここにだけ書かれ、本文には番号で参照させる。

    ``CATALOG_EXCLUDED_KINDS`` の型は**提示しない** (照合には使う)。

    ``max_entries`` を超えるときは優先順 (``_RENDER_PRIORITY``) で残し、**番号は振り直さない**
    — 照合は全件カタログで行うので、提示しなかった番号を LLM が書いても実値に解決できる
    (振り直すと同じ番号が別の値を指す)。IOC を大量に列挙する記事 (2026-09-21 実測:
    カタログ 84k 字 → プロンプト 60k tok、1 件 10 分超 + 出力暴走) への上限。
    """
    shown = [e for e in catalog.entries if e.token]
    if not shown:
        return "(この事象に識別子はありません。本文にも識別子を書かないこと)"
    omitted = 0
    if max_entries is not None and len(shown) > max_entries:
        ranked = sorted(shown, key=lambda e: (_render_rank(e.identifier.kind), int(e.token[1:])))
        kept = {e.token for e in ranked[:max_entries]}
        omitted = len(shown) - max_entries
        shown = [e for e in shown if e.token in kept]  # 元の並び (番号順) を保つ
    lines = [
        f"{e.token} = {e.identifier.raw}  ({e.identifier.kind}, 記事 "
        + "".join(f"[{m}]" for m in sorted(e.members))
        + ")"
        for e in shown
    ]
    if omitted:
        lines.append(f"(他 {omitted} 件の識別子は省略。本文に識別子を直書きしないこと)")
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
    repairs: list[tuple[str, str]] = []
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
            continue
        # 緩い型 (version/cvss) は原則 計数のみ (§C)。ただし**長い値が 1 文字だけ違う**
        # ときは取り違えなので、意図された値へ直す (2026-09-19。審判が接地違反として
        # 挙げた実例 ``151.0.79222.138`` はこの形だった)。
        intended = near_miss_value(ident.normalized, known)
        if intended is not None:
            repairs.append((ident.raw, catalog.raw_of(intended) or intended))
            stats = stats.merged(ResolveStats(literal_repaired=1))
        else:
            stats = stats.merged(ResolveStats(literal_flagged=1))

    stats = stats.merged(ResolveStats(script_anomalies=count_script_anomalies(text)))
    out = _REF_RE.sub(_sub, text)
    leftover = len(_BRACE_RE.findall(out))
    if leftover:
        out = _BRACE_RE.sub(lambda m: m.group(1), out)
        stats = stats.merged(ResolveStats(unwrapped=leftover))
    for raw, intended in repairs:
        out = out.replace(raw, intended)
    for raw in substitutions:
        out = out.replace(raw, UNRESOLVED_PLACEHOLDER)
    return re.sub(r"\s{2,}", " ", out).strip(), stats
