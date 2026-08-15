"""記事タイプ判定 (Phase 5K)。

LLM が `SummaryOutput.article_type` で示す主観判定と、タイトル正規表現の
決定的判定を **合議** して最終 article_type を決める。

役割:
    - 「Weekly Recap」「Top 10 ...」のような digest 系記事を確実に検出
    - LLM 判定が "breaking" でも title regex で "recap" 検出 → recap に格下げ
    - 両ソース (RSS / Grok) で共有可能

RSS 経路: title + body から判定。LLM の article_type を hint として使う。
Grok 経路: GrokSection は元々構造化されているため、ここを通る必要は少ない
           (state_apt summary などはセクション ID で判定可能)。
"""

from __future__ import annotations

import re
from typing import Literal

ArticleType = Literal[
    "breaking",
    "advisory",
    "recap",
    "tutorial",
    "research",
    "press",
    "opinion",
]

# Phase 5K: タイトル正規表現で確実に検出できる article_type パターン。
# LLM 判定より優先される (recap / tutorial を取りこぼさないため)。
_RECAP_PATTERNS = re.compile(
    r"\b(weekly|monthly|quarterly|yearly|"
    r"recap|roundup|wrap.?up|in\s+review|"
    r"highlights\s+of|summary\s+of|"
    r"year[- ]end|month[- ]end|week[- ]end)\b",
    re.IGNORECASE,
)
_TOP_N_PATTERNS = re.compile(
    r"\btop\s+\d+\b|\b\d+\s+best\b|\bbest\s+of\b|\b(\d+)\s+ways\b",
    re.IGNORECASE,
)
_TUTORIAL_PATTERNS = re.compile(
    r"\b(how\s+to|guide\s+to|introduction\s+to|getting\s+started|"
    r"explained|primer|tutorial|cheat\s?sheet)\b",
    re.IGNORECASE,
)
_PRESS_PATTERNS = re.compile(
    r"\b(launches|launches\s+new|releases|releases\s+new|"
    r"announces|introduces|unveils)\b",
    re.IGNORECASE,
)
# 日本語パターン
_JP_RECAP_PATTERNS = re.compile(
    r"(週間|月間|月次|まとめ|総まとめ|総括|振り返り|今週の|今月の|"
    r"上半期|下半期|年末|年初|まとめると)",
)
_JP_TUTORIAL_PATTERNS = re.compile(
    r"(入門|解説|超解説|やさしく|わかりやすく|徹底解説|徹底比較|まるわかり)",
)


def detect_article_type(title: str, llm_hint: str | None = None) -> ArticleType:
    """タイトル regex + LLM hint の合議で article_type を決定する。

    優先順:
        1. タイトル regex で recap / tutorial / press を検出 (高確度)
        2. LLM hint (有効な ArticleType 値なら採用)
        3. fallback: "breaking" (CTI 業務上、未分類は actionable として扱う)
    """
    title_lc = (title or "").strip()
    if not title_lc:
        return _normalize_hint(llm_hint, default="breaking")

    # 1. タイトル regex 優先 (LLM 判定を上書き)
    if _RECAP_PATTERNS.search(title_lc) or _TOP_N_PATTERNS.search(title_lc):
        return "recap"
    if _JP_RECAP_PATTERNS.search(title_lc):
        return "recap"
    if _TUTORIAL_PATTERNS.search(title_lc) or _JP_TUTORIAL_PATTERNS.search(title_lc):
        return "tutorial"
    if _PRESS_PATTERNS.search(title_lc):
        return "press"

    # 2. LLM hint 採用
    return _normalize_hint(llm_hint, default="breaking")


def _normalize_hint(hint: str | None, *, default: ArticleType) -> ArticleType:
    """LLM hint が ArticleType の有効値なら採用、それ以外は default を返す。"""
    if hint is None:
        return default
    cleaned = hint.strip().lower()
    valid: tuple[ArticleType, ...] = (
        "breaking",
        "advisory",
        "recap",
        "tutorial",
        "research",
        "press",
        "opinion",
    )
    for v in valid:
        if cleaned == v:
            return v
    return default
