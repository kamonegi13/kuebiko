"""状態の変化の語・数値の辞書 (日英、重複 alert の特徴量用)。辞書はここ一か所。

由来: data/mlx/r1e_lexicon.py (R1e の測定用) を src 側へ正式に移したもの。
"""

from __future__ import annotations

import re

STATE_LEXICON: dict[str, re.Pattern[str]] = {
    "patch": re.compile(
        r"修正済み|パッチ|修正プログラム|修正版|アップデート(?:を)?(?:公開|提供)"
        r"|\bpatch(?:ed|es)?\b|\bhotfix\b|\bfixed in\b|security update",
        re.I,
    ),
    "exploited": re.compile(
        r"悪用を確認|実際の攻撃|攻撃に悪用|悪用されて|野放し|in the wild|actively exploited"
        r"|exploitation (?:was )?(?:observed|detected|confirmed)|exploited in",
        re.I,
    ),
    "kev": re.compile(r"\bKEV\b|Known Exploited", re.I),
    "attribution": re.compile(
        r"帰属|関与|背後|attribut|linked to|tied to|behind the|responsible for the",
        re.I,
    ),
    "arrest": re.compile(
        r"逮捕|起訴|摘発|送検|\barrest|\bindict|\bcharged\b|extradit|sentenc|pleaded guilty",
        re.I,
    ),
    "expansion": re.compile(
        r"被害(?:が)?拡大|拡大|新たな被害|追加の被害|additional victims|more victims"
        r"|expanded|widespread|(?:also|further) (?:affected|impacted)",
        re.I,
    ),
}

_UNIT = (
    r"件|万|億|千|GB|TB|MB|人|社|台|ドル|円|%|％|組織|拠点|"
    r"million|billion|thousand|users|customers|records|victims|organizations|organisations|devices|servers|"
    r"systems|companies|employees|accounts|GB|TB|MB"
)
_CVE = re.compile(r"CVE-\d{4}-\d{4,}", re.I)
_NUM = re.compile(rf"([$¥￥€]?\d[\d,]*(?:\.\d+)?)\s*({_UNIT})?", re.I)
_TRIVIAL_MAX = 10  # 単位が無い 1 桁〜10 はノイズとして数えない


def numbers_of(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """(単位なしを含む全数値, 単位付き数値) を正規化して返す。CVE 番号は除く。"""
    cleaned = _CVE.sub(" ", text)
    plain: set[str] = set()
    united: set[str] = set()
    for m in _NUM.finditer(cleaned):
        val = m.group(1).replace(",", "").lstrip("$¥￥€")
        unit = (m.group(2) or "").lower()
        try:
            small = float(val) <= _TRIVIAL_MAX
        except ValueError:
            continue
        if unit:
            united.add(f"{val}{unit}")
            plain.add(val)
        elif not small:
            plain.add(val)
    return frozenset(plain), frozenset(united)


def state_hits(text: str) -> frozenset[str]:
    return frozenset(k for k, p in STATE_LEXICON.items() if p.search(text))
