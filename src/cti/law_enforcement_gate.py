"""alert channel: 当局の法執行発表を除外する関門 (R1b、2026-10-11)。

利用者決定 (10-11): 当局による摘発・無力化・起訴・押収・逮捕・制裁指定の発表
(例: FBI が中国系 APT の基盤を無力化) は alert にしない (watch/日次で読む)。
根拠: R1 の 30 日再現 (data/eval/r1_report.md) で alert 対象外の多くがこの型だった。

判定は **決定的な語の条件** (``has_law_enforcement_keyword``) だけ — 高速・無料。
takedown/indict/arrest/seized/disrupt/摘発/起訴/逮捕/無力化 に加え、利用者の元の決定文 (1) が
明示する「押収」も英語 "seized" の対訳として加えた (差し押さえも同義語として追加)。

logprob 判定 (``alert_law_enforcement_judge`` step) は本番の呼び出しが 0 のまま退役した
(2026-10-29、P0-05)。判定は全件「はい」に偏り精度が出なかった。設計と較正の記録は
data/eval/r1b_report.md に残る。

本番適用は alert 候補にのみ (watch へ降格)。他チャンネルの判定は変えない。

rollback: 環境変数 ``ALERT_EXCLUDE_LAW_ENFORCEMENT`` を既定 ("0") のままにすると無効化。
"""

from __future__ import annotations

import os
import re

GATE_ENV = "ALERT_EXCLUDE_LAW_ENFORCEMENT"

#: 英語の決定的な語 (単語境界つき、大小文字不問)。
_KEYWORD_PATTERN_EN: re.Pattern[str] = re.compile(
    r"\b(takedown|take[- ]down|indict\w*|arrest\w*|seiz\w*|disrupt\w*|sanction\w*\s+design\w*)\b",
    re.IGNORECASE,
)

#: 日本語の決定的な語 (部分一致)。利用者の決定文 (1) の 5 語
#: (摘発・無力化・起訴・押収・逮捕) + 「差し押さえ」(押収の同義語) + 「制裁指定」。
_KEYWORD_WORDS_JA: tuple[str, ...] = (
    "摘発",
    "無力化",
    "起訴",
    "押収",
    "差し押さえ",
    "逮捕",
    "制裁指定",
)


def is_law_enforcement_gate_enabled() -> bool:
    """``ALERT_EXCLUDE_LAW_ENFORCEMENT`` (既定 off)。"0" 以外を明示したときだけ有効化する。"""
    value = os.environ.get(GATE_ENV, "0").strip()
    return value not in ("", "0")


def has_law_enforcement_keyword(title: str, summary: str = "") -> bool:
    """決定的な語の条件 (比較用・fallback)。タイトル+要約に 1 語でもあれば True。"""
    text = f"{title} {summary}"
    if _KEYWORD_PATTERN_EN.search(text):
        return True
    return any(word in text for word in _KEYWORD_WORDS_JA)


__all__ = [
    "GATE_ENV",
    "has_law_enforcement_keyword",
    "is_law_enforcement_gate_enabled",
]
