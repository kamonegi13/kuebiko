"""要約の暴走の検出と修復 (2026-09-27)。

暴走 = 構造化出力で同じ文・同じ要素を繰り返し続け、出力上限を使い切る故障。Gemma 4 の
既知の不具合 (SYNTHESIS §59) で、s20 評価では 3.5% (s17 本番は 30 日で 0.02%)。上限まで
繰り返すと **後ろの欄 (被害組織・道具・分析メモ) が全部空** になる。

一覧は maxItems (summary._LIST_MAX_ITEMS) で文法的に止めるが、文字列の繰り返しは文法では
止められない (maxLength は 09-26 に暴走を誘発)。そこで呼び出し側が検出して **1 回だけ作り直す**
(暴走は裾の事象で、引き直せば大半は出ない)。作り直しても暴走したら、繰り返しを畳んで使う
(後ろの欄は戻らないが、繰り返しの文を読者に届けない)。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.pipeline.summary import SummaryOutput

#: 同じ文がこの回数以上出たら暴走 (正当な要約で同じ文を 3 回書くことは無い)
SENTENCE_REPEAT_MIN = 3
#: 一覧で重複がこの数以上なら暴走
LIST_DUP_MIN = 5
#: 文とみなす最短の長さ (短い定型句の偶然の一致を拾わない)
_SENTENCE = re.compile(r"[^。！？!?\n]{12,}[。！？!?]")


def _repeated_sentences(text: str) -> int:
    counts = Counter(s.strip() for s in _SENTENCE.findall(text))
    return counts.most_common(1)[0][1] if counts else 0


def runaway_reasons(summary: SummaryOutput) -> list[str]:
    """暴走の兆候 (欄名と種類)。空なら正常 (純粋関数)。"""
    reasons: list[str] = []
    for name, value in summary.model_dump().items():
        if isinstance(value, str) and _repeated_sentences(value) >= SENTENCE_REPEAT_MIN:
            reasons.append(f"{name}:sentence_repeat")
        elif isinstance(value, list) and len(value) - len({str(v) for v in value}) >= LIST_DUP_MIN:
            reasons.append(f"{name}:list_dup")
    return reasons


def _collapse_sentences(text: str) -> str:
    """同じ文の 2 回目以降を落とす (初出の順を保つ)。"""
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"(?<=[。！？!?])", text):
        key = part.strip()
        if len(key) >= 12 and key in seen:
            continue
        seen.add(key)
        out.append(part)
    return "".join(out).strip()


def repair(summary: SummaryOutput) -> SummaryOutput:
    """繰り返しを畳んだ新しい要約 (元は変更しない)。"""
    update: dict[str, Any] = {}
    for name, value in summary.model_dump().items():
        if isinstance(value, str) and _repeated_sentences(value) >= SENTENCE_REPEAT_MIN:
            update[name] = _collapse_sentences(value)
        elif isinstance(value, list) and len(value) - len({str(v) for v in value}) >= LIST_DUP_MIN:
            update[name] = list(dict.fromkeys(value))
    return summary.model_copy(update=update) if update else summary
