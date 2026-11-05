"""claim の忠実さの決定論の関門と、単発の事案を開設しない絞り込み (DETECT-WRITER-FIX、既定 off)。

DETECT-JUDGE (2026-11-04、不合格) の Opus 裁定で、分けた形の誤りは (a) claim が根拠どおりでない
(Q3) と (b) 追跡の価値が無い単発の事案 (Q2) に偏った。型と件数は
data/eval/detect_writer_errors_2026_11_05.md。これは**結果を見た後の探索**であり、ここの規則は
再判定 (新しい窓・Opus) で確かめるまで本番に使わない (``DETECT_WRITER_GATES``、既定 0)。

``claim_grounding`` (固有名詞の関門) が見ない型を足す:

- **2 文以上**: プロンプトは claim 1 文。2 文目に付け足し (検知日・再浮上・他社の話) が入る。
- **根拠に無い月日**: claim の ``M月D日`` が塊の見出し・要約・本文冒頭のどこにも無い。
- **根拠に無い「再浮上/再燃」**: 追跡の動機を作る付け足し。
- **出典名の付け足し**: 見出し・要約に無い媒体名・投稿者名を claim に書く (本文冒頭の
  言及だけでは裏付けにしない)。

単発の事案 (Q2) は既存の部品を再利用する: 当局の摘発・逮捕発表の語
(``cti.law_enforcement_gate.has_law_enforcement_keyword``) とまとめ記事 (``cti.roundup_detector``)。
塊の記事が**すべて**該当するときだけ開設しない (複数の独立した報道が続報を示す塊は残す)。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from src.assessment.claim_grounding import GroundingSources
from src.cti.law_enforcement_gate import has_law_enforcement_keyword
from src.cti.roundup_detector import is_roundup

#: 「。」の直後が閉じ括弧・引用符のものは文の区切りにしない (「…した。」と述べた)
_SENTENCE_BREAK = re.compile("。\\s*[^\\s」』）)］】〉》”’\"']")
_MONTH_DAY = re.compile(r"(\d{1,2})月(\d{1,2})日")
_SLASH_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
_ISO_DATE = re.compile(r"\b\d{4}[-/](\d{1,2})[-/](\d{1,2})\b")
_MONTH_NAMES = [
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
_MONTH_ALT = "|".join(f"{m}|{m[:3]}" for m in _MONTH_NAMES)
_EN_MONTH_DAY = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.IGNORECASE)
_EN_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_ALT})\b", re.IGNORECASE
)
_MAX_MONTH, _MAX_DAY = 12, 31
#: 根拠に無ければ付け足しとみなす語 (言い換えの余地が小さい語だけ)
_UNSUPPORTED_MARKERS: tuple[str, ...] = ("再浮上", "再燃")
_MIN_NAME_CHARS = 4
#: 出典名の分解語から除く一般語 (これだけでは媒体を特定しない)
_GENERIC_OUTLET_WORDS = frozenset(
    {"security", "news", "record", "recorded", "future", "blog", "daily", "cyber", "weekly"}
)
_LATIN_WORD = re.compile(r"[A-Za-z0-9]+")


def _fold(s: str) -> str:
    folded = unicodedata.normalize("NFKC", s).casefold()
    return "".join(ch for ch in folded if ch.isalnum() or "ぁ" <= ch <= "ヿ")


def _month_number(name: str) -> int:
    key = name.casefold()[:3]
    return next(i + 1 for i, m in enumerate(_MONTH_NAMES) if m.startswith(key))


def _month_days(s: str) -> frozenset[tuple[int, int]]:
    """文中の日付を (月, 日) にそろえる (M月D日・M/D・ISO・Nov 3・3 November)。"""
    text = unicodedata.normalize("NFKC", s)
    found: list[tuple[int, int]] = []
    for pat in (_MONTH_DAY, _SLASH_DATE, _ISO_DATE):
        found.extend((int(m), int(d)) for m, d in pat.findall(text))
    found.extend((_month_number(m), int(d)) for m, d in _EN_MONTH_DAY.findall(text))
    found.extend((_month_number(m), int(d)) for d, m in _EN_DAY_MONTH.findall(text))
    return frozenset((m, d) for m, d in found if 1 <= m <= _MAX_MONTH and 1 <= d <= _MAX_DAY)


def _outlet_names(attribution: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """出典欄 (媒体名・投稿者名) の照合名: (全体の正規化形, 一般語を除いた英字の語)。"""
    fulls: list[str] = []
    words: list[str] = []
    for line in attribution.splitlines():
        full = _fold(line)
        if len(full) >= _MIN_NAME_CHARS:
            fulls.append(full)
        for w in _LATIN_WORD.findall(unicodedata.normalize("NFKC", line)):
            key = w.casefold()
            if len(key) >= _MIN_NAME_CHARS and key not in _GENERIC_OUTLET_WORDS:
                words.append(key)
    return tuple(dict.fromkeys(fulls)), tuple(dict.fromkeys(words))


def check_fidelity(claim: str, sources: GroundingSources) -> tuple[str, ...]:
    """claim の忠実さの違反。

    ``multi_sentence`` / ``date:M月D日`` / ``unsupported:語`` / ``outlet:名``。
    """
    out: list[str] = []
    if _SENTENCE_BREAK.search(claim.strip()):
        out.append("multi_sentence")
    unknown_days = _month_days(claim) - _month_days(sources.text)
    out.extend(f"date:{m}月{d}日" for m, d in sorted(unknown_days))
    out.extend(
        f"unsupported:{w}" for w in _UNSUPPORTED_MARKERS if w in claim and w not in sources.text
    )
    claim_key = _fold(claim)
    headline_key = _fold(sources.headline or sources.text)
    fulls, words = _outlet_names(sources.attribution)
    claim_words = {w.casefold() for w in _LATIN_WORD.findall(unicodedata.normalize("NFKC", claim))}
    named = [n for n in fulls if n in claim_key] + [n for n in words if n in claim_words]
    out.extend(f"outlet:{n}" for n in dict.fromkeys(named) if n not in headline_key)
    return tuple(out)


def one_off_reason(members: Sequence[tuple[str, str]]) -> str | None:
    """塊が単発の事案なら理由 (``law_enforcement`` / ``roundup``)、そうでなければ None。

    ``members`` = 記事ごとの (見出し, 要約)。**全記事**が該当するときだけ。
    """
    if not members:
        return None
    if all(has_law_enforcement_keyword(t, s) for t, s in members):
        return "law_enforcement"
    if all(is_roundup(t) for t, _ in members):
        return "roundup"
    return None
