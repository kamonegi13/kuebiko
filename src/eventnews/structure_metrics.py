"""構成の質を決定論で測る (2026-09-20)。

⚠ 「接地が退行の軸」という §53 以来の説明は**独立採点で崩れた** — 同時採点の審判は
接地違反で勝敗を 12/12 で完全予測していたが、相手を見ずに採点すると 5/11 (偶然水準)
に落ちた。審判が結論を先に立てて観点を後から合わせていた (事後合理化)。

代わりに Opus の自由記述 (優劣を強制しない設計) が挙げたのは**構成**だった:

- 節 (what / scope / how / …) への振り分けが偏り、一色化する
- **相違欄に同一文が重複し、しかも自己矛盾する**
  (「数値自体は一致するが」と書きながら相違として挙げる)

⭐ **審判に依存しない指標にできるものは、指標にする**。上の 2 つは機械で数えられる。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Protocol


class _Line(Protocol):
    text: str
    section: str


# 「相違」と称しながら一致を認めている言い回し。⚠ 網羅は狙わない — 実データで
# 観測したものだけを入れ、増えたら足す (推測で広げると誤検出が増える)。
_AGREEMENT_PHRASES: tuple[str, ...] = (
    "一致する",
    "一致している",
    "同じ値",
    "同一の値",
    "同じ内容",
    "差異はない",
    "違いはない",
    "相違はない",
)


def _norm(text: str) -> str:
    """照合用の正規化 — 空白・句読点・全角半角の違いで取りこぼさない。"""
    s = unicodedata.normalize("NFKC", text)
    return re.sub(r"[\s。、,.]", "", s)


def section_concentration(lines: Sequence[_Line]) -> float:
    """最も多い節が占める割合 (0 から 1)。1 に近いほど一色化している。

    節を使い分けず 1 つに寄せると読み物として辿りにくい。Opus が最初に挙げた違い。
    """
    if not lines:
        return 0.0
    counts: dict[str, int] = {}
    for line in lines:
        key = getattr(line, "section", "") or "what"
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values()) / len(lines)


def duplicate_lines(lines: Sequence[_Line]) -> int:
    """正規化して一致する行の重複数 (2 件あれば 1、3 件あれば 2)。"""
    seen: set[str] = set()
    dup = 0
    for line in lines:
        key = _norm(line.text)
        if not key:
            continue
        if key in seen:
            dup += 1
        else:
            seen.add(key)
    return dup


def self_contradicting_discrepancies(lines: Sequence[_Line]) -> int:
    """「相違」と称しながら一致を認めている行の数。

    実例 (2026-09-20 に Opus が指摘): 「一方は…もう一方は…**数値自体は一致するが**
    表現が異なる」を相違として 2 件重複で挙げていた。
    """
    return sum(1 for line in lines if any(p in line.text for p in _AGREEMENT_PHRASES))


def duplicate_hint(fields: Mapping[str, Sequence[_Line]]) -> str | None:
    """重複している行を名指しする書き直し指示 (純粋関数)。``None`` なら直すものが無い。

    ⚠ 「重複を消せ」だけでは効かない — 08-27 実測で **31B は散文の指示を読まず構造だけが
    効く**。08-27 の識別子関門は「具体的な番号の指摘」で不足 5 → 0 に埋めた。同じ形にする。
    """
    named: list[str] = []
    for field, lines in fields.items():
        # ⭐ 示すのは**最初の出現**。2 度目以降の表記揺れを見せても直しにくい
        first: dict[str, str] = {}
        dups: list[str] = []
        for line in lines:
            key = _norm(line.text)
            if not key:
                continue
            if key in first:
                if first[key] not in dups:
                    dups.append(first[key])
            else:
                first[key] = line.text
        named += [f"{field}: 「{t[:80]}」" for t in dups]
    if not named:
        return None
    return (
        "前回の出力には**同じ内容の行が複数**含まれていた: "
        + " / ".join(named[:6])
        + "。**同じ主張を 2 度書かない**。重複を 1 件にまとめ、"
        "他の内容の質は保ったまま書き直すこと。"
    )
