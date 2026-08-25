"""注目 (featured) の選定 — **読み手が今いちばん知る必要がある事案**を選ぶ。

2026-08-25 改訂。それまでは独立媒体数の多い順だったが、**報道量は注意の量であって
重要性ではない**。CLAUDE.md §7「収集量を重要性の代理にしない」に照らして作り直した。

実測 (公開対象 high 822 件) が示したこと:

- 日本関連は **4.5% (37 件)** しかない。媒体数順ではまず選ばれないが、利用者の
  任務では最重要
- 一次情報 (政府・CERT) は **2.8%**。権威ある発表ほど報じる媒体は少ない
- 逆に英語圏の大手が扱う海外製品の脆弱性は 10 媒体を超える

そこで「行動を要する度合い」で点を付ける。**媒体数は使わない** (同点処理にも使わない
— 使えば結局は量が順位を決める)。

配点の根拠:
- 実悪用 (100) — 「今すぐ対処が要る」の最強の信号。実測で 21.0% が該当
- 日本関連 (60) — 読み手の所在。単独でも CVSS 9.0+ を上回る重みにする
- CVSS 9.0+ (40) / 7.0+ (15) — 深刻度。CVSS が付くのは 12.8% だけなので副次的
- ランサム (20) — 実被害が出ている
- PIR 一致 (各 10、上限 3 件) — ツール自身の「追う」という定義
"""

from __future__ import annotations

import re
from typing import Any

# 「実際に悪用されている」ことを示す語。日英・簡体/繁体の報道を想定する。
_EXPLOITED_PATTERNS = (
    r"実際に悪用",
    r"実悪用",
    r"悪用が確認",
    r"悪用を確認",
    r"悪用が観測",
    r"悪用されている",
    r"悪用されていた",
    r"悪用が拡大",
    r"ゼロデイ攻撃",
    r"ゼロデイ",
    r"KEV",
    r"Known Exploited",
    r"actively exploited",
    r"active exploitation",
    r"exploited in the wild",
    r"in-the-wild exploitation",
    r"zero-day",
)
# ⚠ 「悪用される**可能性**」を拾うと過検出になる。一致の周辺にこれがあれば数えない。
_HYPOTHETICAL = r"(可能性|恐れ|おそれ|懸念|could|may be|potential|risk of|悪用されると)"
_WINDOW_CHARS = 40

_EXPLOITED_RE = re.compile("|".join(_EXPLOITED_PATTERNS), re.I)
_HYPOTHETICAL_RE = re.compile(_HYPOTHETICAL, re.I)

# 配点 (docstring に根拠)
SCORE_EXPLOITED = 100
SCORE_JAPAN = 60
SCORE_CVSS_CRITICAL = 40
SCORE_CVSS_HIGH = 15
SCORE_RANSOMWARE = 20
SCORE_PER_PIR = 10
MAX_PIR_COUNTED = 3

# 注目に置かない分類。まとめ記事は複数の話題を含むため信号が同時に立ち、
# 単一の事案より高い点が付いてしまう (実測で週刊まとめが 3 位に入った)。
EXCLUDED_CATEGORIES = frozenset({"recap"})


def looks_exploited(text: str) -> bool:
    """本文が「実際に悪用されている」と述べているか (可能性の話は除く)。"""
    for match in _EXPLOITED_RE.finditer(text or ""):
        around = text[max(0, match.start() - _WINDOW_CHARS) : match.end() + _WINDOW_CHARS]
        if _HYPOTHETICAL_RE.search(around):
            continue
        return True
    return False


def urgency_score(
    *,
    text: str,
    max_cvss: float,
    japan_related: bool,
    ransomware: bool,
    pir_count: int,
) -> int:
    """行動を要する度合い。**媒体数は入れない**。"""
    score = 0
    if looks_exploited(text):
        score += SCORE_EXPLOITED
    if japan_related:
        score += SCORE_JAPAN
    if max_cvss >= 9.0:
        score += SCORE_CVSS_CRITICAL
    elif max_cvss >= 7.0:
        score += SCORE_CVSS_HIGH
    if ransomware:
        score += SCORE_RANSOMWARE
    score += min(max(pir_count, 0), MAX_PIR_COUNTED) * SCORE_PER_PIR
    return score


def is_excluded_category(articles: list[Any]) -> bool:
    """まとめ記事など、注目に置かない分類か。"""
    return any((getattr(a, "category", "") or "") in EXCLUDED_CATEGORIES for a in articles)
