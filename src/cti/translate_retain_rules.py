"""翻訳で「正しく訳しても原文のまま残る行」の追加規則 (タグ行・コード行)。

背景 (2026-11-18): 原文の写し判定 (TranslationEchoError) で恒常的に失敗する記事の写し行は、
大半が写しで正しい行だった (記事末尾のカンマ区切りのタグ行、既存の規則をすり抜けるコード行)。
本モジュールがその判定の SSoT で、写し判定 (body_translator) と保持行の素通し
(translate_passthrough) の両方が参照する。

規則は保守的に作る: 散文を保持行にすると訳が消えるので、取りこぼす側に倒す。
``TRANSLATE_RETAIN_LINES_V2=0|shadow|1`` (コードの既定 0)。本文はログに出さない。

見送り: 記事末尾の関連記事のタイトル列 (タイトルケースの短い行の連続)。見出し・小見出し・
固有名詞だけの散文の行と形で区別できず、誤検出すると訳が消えるため。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

RETAIN_LINES_V2_ENV = "TRANSLATE_RETAIN_LINES_V2"

KIND_TAG = "tag_line_v2"
KIND_CODE = "code_line_v2"

_TAG_MIN_CHARS = 30
_TAG_MIN_ELEMENTS = 4
_TAG_MAX_WORDS_PER_ELEMENT = 6
_TAG_MAX_MEAN_WORDS = 3.0
_TAG_LOWER_RATIO = 0.8
_NON_LATIN_MAX_RATIO = 0.05
_TAG_STOPWORDS = frozenset(
    [
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "may",
        "might",
        "that",
        "which",
        "who",
        "and",
        "but",
        "or",
        "said",
        "says",
        "say",
        "not",
    ]
)
_SENTENCE_END_RE = re.compile(r"[.!?。！？:;]\s*$")

_CODE_MIN_SCORE = 2
_CODE_MIN_SYMBOL_RATIO = 0.07
_PROSE_WORD_RATIO = 0.6
_PROSE_MIN_TOKENS = 8
_PLAIN_WORD_RE = re.compile(r"^[A-Za-z]+[.,]?$")
# (名前, 規則)。1 つ当たるごとに 1 点
_CODE_SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Za-z0-9]+_[A-Za-z0-9_]+"),  # snake_case / 定数 / 型名 (_t)
    re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b"),  # camelCase
    re.compile(r"\b0x[0-9a-fA-F]+"),
    re.compile(r"->|::|=>|==|!=|&&|\|\|"),
    re.compile(r"\$\w"),
    re.compile(r"\w\s*\*\s*\w"),  # ポインタ宣言
    re.compile(r'"""|//|/\*|\*/'),
    re.compile(r"\w\.\w+\s=\s\S"),  # 代入 (a.b = x)
    re.compile(r"[;{}]\s*$"),
    re.compile(r"^\s*\|\s*\S"),  # パイプ行頭 (KQL / ES|QL)
    re.compile(r"^\s*\d+:\s"),  # 行番号つきの抜粋
    re.compile(r"^\s*[$>]\s+\S"),  # シェルのプロンプト
)
_SYMBOL_RE = re.compile(r"[^\w\s]")
_COMMENT_LEAD_RE = re.compile(r"^\s*#(?:!|\s+\S)")
_URL_RE = re.compile(r"(?:https?|hxxps?)://\S+", re.IGNORECASE)
_FUNCTION_WORDS = frozenset(
    [
        "the",
        "of",
        "and",
        "to",
        "in",
        "for",
        "with",
        "that",
        "you",
        "what",
        "on",
        "is",
        "are",
        "was",
        "this",
        "it",
        "as",
        "by",
        "at",
        "from",
    ]
)
_PROSE_MIN_FUNCTION_WORDS = 3
_NEIGHBOR_REACH = 1


def retain_lines_v2_mode() -> str:
    """``TRANSLATE_RETAIN_LINES_V2`` (0 / shadow / 1、既定 0)。未知の値は 0。"""
    raw = os.environ.get(RETAIN_LINES_V2_ENV, "0").strip().lower()
    if raw in ("1", "true", "on"):
        return "1"
    if raw == "shadow":
        return "shadow"
    return "0"


def _non_latin_ratio(s: str) -> float:
    """ラテン文字の範囲 (と引用符・ダッシュ) の外の文字の割合 (日本語の行を除くため)。"""
    return sum(ord(c) > 0x24F and c not in "„“”‘’–—❯" for c in s) / len(s)


def _is_tag_line(line: str) -> bool:
    """カンマ区切りの小文字の語の列 (記事末尾のタグ・メタ行)。文末記号なし・動詞的な語なし。"""
    s = line.strip()
    if not s[:1].islower() or ":" in s:
        return False
    if len(s) < _TAG_MIN_CHARS or _SENTENCE_END_RE.search(s):
        return False
    if _non_latin_ratio(s) > _NON_LATIN_MAX_RATIO:
        return False
    elements = [e.strip() for e in s.split(",")]
    if len(elements) < _TAG_MIN_ELEMENTS or any(not e for e in elements):
        return False
    sizes = [len(e.split()) for e in elements]
    if max(sizes) > _TAG_MAX_WORDS_PER_ELEMENT or sum(sizes) / len(sizes) > _TAG_MAX_MEAN_WORDS:
        return False
    words = re.findall(r"[A-Za-z']+", s.lower())
    if any(w in _TAG_STOPWORDS for w in words):
        return False
    lower = sum(e[0].islower() for e in elements if e[0].isalpha())
    alpha = sum(e[0].isalpha() for e in elements)
    return alpha >= _TAG_MIN_ELEMENTS and lower / alpha >= _TAG_LOWER_RATIO


def _is_code_line(line: str) -> bool:
    """識別子・記号の密度が高い行 (単独で判定できるもの)。散文らしい行は除く。"""
    s = _URL_RE.sub("", line).strip()
    if not s or _non_latin_ratio(s) > _NON_LATIN_MAX_RATIO:
        return False
    function_words = sum(w in _FUNCTION_WORDS for w in re.findall(r"[a-z]+", s.lower()))
    if function_words >= _PROSE_MIN_FUNCTION_WORDS:
        return False
    score = sum(1 for sig in _CODE_SIGNALS if sig.search(s))
    if score < _CODE_MIN_SCORE:
        return False
    if len(_SYMBOL_RE.findall(s)) / len(s) < _CODE_MIN_SYMBOL_RATIO:
        return False
    tokens = s.split()
    plain = sum(1 for t in tokens if _PLAIN_WORD_RE.match(t))
    return not (len(tokens) >= _PROSE_MIN_TOKENS and plain / len(tokens) > _PROSE_WORD_RATIO)


def classify_lines(lines: Sequence[str]) -> list[str | None]:
    """各行の追加規則の種類 (``KIND_TAG`` / ``KIND_CODE``)。当たらなければ None (純粋関数)。

    ``# …`` の行はマークダウンの見出しと区別できないので、隣の行がコード行のときだけコードとみなす。
    """
    kinds: list[str | None] = [None] * len(lines)
    for i, line in enumerate(lines):
        if _is_code_line(line):
            kinds[i] = KIND_CODE
        elif _is_tag_line(line):
            kinds[i] = KIND_TAG
    for i, line in enumerate(lines):
        if kinds[i] or not _COMMENT_LEAD_RE.match(line):
            continue
        near = range(max(0, i - _NEIGHBOR_REACH), min(len(lines), i + _NEIGHBOR_REACH + 1))
        if any(j != i and kinds[j] == KIND_CODE for j in near):
            kinds[i] = KIND_CODE
    return kinds
