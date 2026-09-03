"""生成本文の数量・日付が原文に実在するかを測る。

識別子関門 (CVE / IP / ハッシュ / バージョン / CVSS) は**数量と日付を見ていない**。
そこが抜けているため、モデルが推論した値が「報じられている内容」として出た::

    原文 「6 つの ISP で最大 1,000 万件」「主要な telco でさらに 800 万件」
    生成 「日本の複数ISPで最大2,620万件」        ← 足し算の結果。原文のどこにも無い

    原文 「8月23日に公開されたレポート」
    生成 「2026年8月23日に公表した報告書」       ← 年を補った

事実に著作権は無い一方で、**事実の正確さは製品の中身そのもの**なので、逐語一致より
実害が直接的になる。表記揺れで正当な値を落とすと精度を下げるので、照合は
「万・億の展開」「桁区切り」「日付の書式」を吸収してから行う。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

from src.eventnews.models import FactItem, MemberArticle

# 数量とみなす単位。単位の無い裸の数値は誤検出が多いので採らない
# (「12 波」「8 体」のような助数詞は原文の言い回し次第で表記が変わる)。
_UNITS = "件|人|台|社|個|回|名|校|万件|億円|円|ドル|ユーロ|％|%|アカウント|キー|組織|端末"
# ⚠ 先頭の lookbehind: 「1万3131件」のような複合数詞の尾 (3131件) を独立した数値と
# して拾わない (2026-09-03 — 実値 13,131 件を「3131件」と誤抽出した)。複合数詞は
# 拾えなくなるが、**誤った値を出すより取り逃す方が安全**。
_QUANTITY_RE = re.compile(rf"(?<![\d万億,.])(\d[\d,]*(?:\.\d+)?)\s*(万|億)?\s*({_UNITS})")
_DATE_FULL_RE = re.compile(r"(20\d\d)年(\d{1,2})月(\d{1,2})日")
_DATE_MD_RE = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日")
# 英語記事が数詞で書く範囲。これを超える数は算用数字で書かれる。
_NUMBER_WORDS: dict[int, str] = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
    20: "twenty",
    30: "thirty",
    40: "forty",
    50: "fifty",
    60: "sixty",
    70: "seventy",
    80: "eighty",
    90: "ninety",
    100: "hundred",
}

_MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]


def _normalize(text: str) -> str:
    return re.sub(r"[\s,]", "", unicodedata.normalize("NFKC", text or "")).casefold()


def _quantity_forms(digits: str, scale: str | None) -> tuple[str, ...]:
    """1 つの数量がとりうる表記。

    ⚠ **言語をまたいで照合する**。プロンプトへ渡すのは原文 (多くは英語) で、生成は
    日本語なので、「14.2 million」→「1420万件」は正しい翻訳であって捏造ではない。
    値へ直してから英語表記も候補に加えないと、正しい行を落とす
    (2026-08-26 実測: これを入れないと 27 行中の大半が誤検出だった)。
    """
    plain = digits.replace(",", "")
    forms = {plain, f"{plain}{scale}" if scale else plain}
    try:
        value = float(plain) * (10000 if scale == "万" else 100_000_000 if scale else 1)
    except ValueError:
        return tuple(forms)
    if value.is_integer():
        forms.add(str(int(value)))
    for unit, size in (("billion", 1_000_000_000), ("million", 1_000_000), ("thousand", 1_000)):
        if value >= size:
            forms.add(f"{value / size:g}{unit}")
    # 小さい数は英語記事が数詞で書く ("killing three people" / "Seven more children")。
    # 数字だけで探すと正しい行を落とす (2026-08-26 実測: 残った誤検出の主因)。
    word = _NUMBER_WORDS.get(int(value)) if value.is_integer() else None
    if word:
        forms.add(word)
    return tuple(forms)


def _date_forms(year: int | None, month: int, day: int) -> tuple[str, ...]:
    """1 つの日付がとりうる表記。

    ⚠ **省略形のピリオドを候補に含める**。英語記事は "Aug. 24" と書くことがあり、
    正規化で空白しか落とさないと "aug.24" になって "aug24" と一致しない
    (2026-08-26 実測: 日付の誤検出の大半がこれだった)。ピリオドを一律に落とすと
    "14.2 million" が "142million" になって数量側の照合が壊れるので、
    **落とすのではなく候補を増やす**。
    """
    name = _MONTHS[month - 1]
    short = name[:3]
    forms = {
        f"{month}月{day}日",
        f"{name}{day}",
        f"{day}{name}",
        f"{short}{day}",
        f"{short}.{day}",
        f"{day}{short}",
        f"{day}{short}.",
    }
    if year is not None:
        forms |= {
            f"{year}年{month}月{day}日",
            f"{year}-{month:02d}-{day:02d}",
            f"{month}/{day}/{year}",
            f"{day}/{month}/{year}",
            f"{name}{day}{year}",
        }
    return tuple(_normalize(f) for f in forms)


def extract_claims(text: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """(表示用の値, 原文で探す表記の候補) の列を返す。"""
    claims: list[tuple[str, tuple[str, ...]]] = []
    for match in _QUANTITY_RE.finditer(text):
        digits, scale, unit = match.groups()
        claims.append((match.group(0), _quantity_forms(digits, scale)))
    seen_spans: set[tuple[int, int]] = set()
    for match in _DATE_FULL_RE.finditer(text):
        year, month, day = (int(g) for g in match.groups())
        seen_spans.add(match.span())
        claims.append((match.group(0), _date_forms(year, month, day)))
    for match in _DATE_MD_RE.finditer(text):
        if any(s <= match.start() and match.end() <= e for s, e in seen_spans):
            continue
        month, day = (int(g) for g in match.groups())
        claims.append((match.group(0), _date_forms(None, month, day)))
    return tuple(claims)


def supporting_texts(members: Sequence[MemberArticle]) -> tuple[str, ...]:
    """照合に使う原文側のテキスト。**プロンプトへ渡した範囲と一致させる**。

    本文だけでなくタイトル・要約・記事時刻 (anchor) も含める。プロンプトはこれらも
    渡しており、「報告日は…」のような記述の根拠になっている。見たいのは
    「渡していない値を書いたか」なので、渡した範囲で照合しないと誤検出になる。
    """
    return tuple(f"{m.title}\n{m.summary}\n{m.body}\n{m.anchor_ts.isoformat()}" for m in members)


#: 割合は「事実が増えた」の指標にならない (「100%」「0.5%」は分析の比であって、
#: 事象そのものの規模ではない)。実データ 2,091 合流の目視で雑音はここに集中していた。
_RATIO_UNITS = ("%", "％")


def new_values(text: str, prior_texts: Sequence[str]) -> tuple[str, ...]:
    """``text`` の数量のうち、既存メンバーのどこにも無いもの (**割合と日付は除く**)。

    ⭐ 続報で「事実が増えたか」を決定論で言うための材料 (2026-09-02)。
    文面の差分は採らない — 生成は毎回書き直されるので一致率は内容の異同を測らない
    (``version_diff`` の冒頭と同じ理由)。原文に書かれた数値だけを見る。

    ⚠ 日付を含めない。実測では大半が公開日・報道日で、事象についての新事実ではない
    (2,091 合流で日付込み 15.9% → 数量のみ 12.8%、差はほぼ日付の雑音)。
    """
    if not prior_texts:
        return ()
    haystack = " ".join(_normalize(b) for b in prior_texts if b)
    if not haystack:
        return ()
    found: list[str] = []
    for match in _QUANTITY_RE.finditer(text):
        digits, scale, unit = match.groups()
        if unit in _RATIO_UNITS:
            continue
        if not any(_normalize(f) in haystack for f in _quantity_forms(digits, scale)):
            found.append(match.group(0))
    return tuple(dict.fromkeys(found))


def unsupported(text: str, bodies: Sequence[str]) -> tuple[str, ...]:
    """``text`` の数量・日付のうち、どの原文にも現れないもの。

    ``bodies`` には照合対象の原文を**全件**渡す (出典番号がずれていても、事象の
    どこかに在れば捏造ではない — 番号のずれは識別子関門が別途扱う)。
    """
    if not bodies:
        return ()
    haystack = " ".join(_normalize(b) for b in bodies)
    missing: list[str] = []
    for shown, forms in extract_claims(text):
        if not any(_normalize(f) in haystack for f in forms):
            missing.append(shown)
    return tuple(missing)


def unsupported_lines(
    facts: Sequence[FactItem], bodies: Mapping[int, str]
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """原文に無い数量・日付を含む行 (位置, 値) の列。"""
    sources = [b for b in bodies.values() if b]
    if not sources:
        return ()
    found: list[tuple[int, tuple[str, ...]]] = []
    for index, fact in enumerate(facts):
        missing = unsupported(fact.text, sources)
        if missing:
            found.append((index, missing))
    return tuple(found)
