"""純関数: 主張の数え方・引用の実在・κ・指摘の再現・主張 100 件あたりの率・ブートストラップ。

主張 = 出力の文 (「。」「！」「？」・改行で区切る、6 字未満の断片は数えない)。審査役に数えさせない
(数えるたびに変わる物差しを避ける、D §2.3)。
"""

from __future__ import annotations

import random
import re
import unicodedata
from collections import Counter
from collections.abc import Hashable, Sequence
from collections.abc import Set as AbstractSet

MIN_CLAIM_CHARS = 6
#: 引用の実在: 正規化した引用の文字 2-gram のうち出力の 1 文に含まれる割合がこれ以上なら実在とみなす
QUOTE_CONTAIN_MIN = 0.8
#: 重さの重み (報告用。合否は重い違反だけで決める、契約 §1)
WEIGHTS = {"重い": 1.0, "軽い": 0.25}

_SPLIT = re.compile(r"(?<=[。！？!?])|\n+")
_MD = re.compile(r"^[\s#>*\-・•]+|\*\*")


def normalize(text: str) -> str:
    """NFKC・空白除去・小文字化 (引用の照合用)。"""
    t = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", "", t).lower()


def split_claims(text: str) -> list[str]:
    out: list[str] = []
    for piece in _SPLIT.split(text):
        p = _MD.sub("", piece or "").strip()
        if len(normalize(p)) >= MIN_CLAIM_CHARS:
            out.append(p)
    return out


def _bigrams(s: str) -> list[str]:
    return [s[i : i + 2] for i in range(len(s) - 1)] if len(s) > 1 else [s]


def containment(quote: str, claim: str) -> float:
    """引用の 2-gram のうち claim に現れる割合 (0〜1)。"""
    q, c = normalize(quote), normalize(claim)
    if not q:
        return 0.0
    if q in c:
        return 1.0
    grams = _bigrams(q)
    cset = set(_bigrams(c))
    return sum(g in cset for g in grams) / len(grams)


def locate_quote(quote: str, claims: Sequence[str]) -> tuple[bool, int | None]:
    """(実在するか, 最もよく含む文の番号)。どの文とも重ならなければ (False, None)。

    文をまたぐ引用は、全文の連結に完全一致すれば実在とみなす。
    """
    if not claims:
        return False, None
    scores = [containment(quote, c) for c in claims]
    best = max(range(len(claims)), key=lambda i: scores[i])
    if scores[best] <= 0:
        return False, None
    q = normalize(quote)
    spans = bool(q) and q in normalize("".join(claims))
    return scores[best] >= QUOTE_CONTAIN_MIN or spans, best


def cohen_kappa(a: Sequence[Hashable], b: Sequence[Hashable]) -> float | None:
    """2 回の審査のラベル列の Cohen の κ。期待一致が 1 (全件同じラベル) のときは None。"""
    if len(a) != len(b):
        raise ValueError("長さが違う")
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if pe >= 1.0:
        return None
    return (po - pe) / (1 - pe)


def agreement(a: Sequence[Hashable], b: Sequence[Hashable]) -> float | None:
    n = len(a)
    return sum(x == y for x, y in zip(a, b, strict=True)) / n if n else None


def reproduction(
    first: AbstractSet[Hashable], second: AbstractSet[Hashable]
) -> dict[str, float | int | None]:
    """指摘単位の再現 (文の番号の集合で照合)。recall = 1 回目の指摘のうち 2 回目も挙げた割合。"""
    both = first & second
    union = first | second
    return {
        "n_first": len(first),
        "n_second": len(second),
        "n_both": len(both),
        "recall_of_first": len(both) / len(first) if first else None,
        "jaccard": len(both) / len(union) if union else None,
    }


def per_100(violations: float, claims: int) -> float | None:
    return round(100.0 * violations / claims, 3) if claims else None


def bootstrap_ci(
    pairs: Sequence[tuple[float, int]], *, seed: int, b: int = 2000, alpha: float = 0.05
) -> tuple[float, float] | None:
    """事例単位の再抽出で、主張 100 件あたりの率の (下限, 上限)。pairs = (違反数, 主張数)。"""
    if not pairs:
        return None
    rng = random.Random(seed)
    n = len(pairs)
    stats: list[float] = []
    for _ in range(b):
        draw = [pairs[rng.randrange(n)] for _ in range(n)]
        claims = sum(c for _, c in draw)
        stats.append(100.0 * sum(v for v, _ in draw) / claims if claims else 0.0)
    stats.sort()
    lo = stats[int(alpha / 2 * b)]
    hi = stats[min(b - 1, int((1 - alpha / 2) * b))]
    return round(lo, 3), round(hi, 3)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """二項の比率の Wilson 区間。"""
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)
