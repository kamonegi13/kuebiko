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
#: 短い型 (2〜20 文字・空白だけでない) がこの回数以上 **連続** したら暴走 (「-n-n-n…」型。
#: 2026-09-27 の s20 評価で 4,213 字の要約の末尾が「-n」の数百回の反復だった)。
#: ⚠ 1 文字の連続は拾わない: 本番 30 日で Solana のアドレス (So111…1) と転載本文の空白の
#: 連続を誤検出した
CHAR_LOOP_MIN = 20
_CHAR_LOOP = re.compile(r"(.{2,20}?)\1{" + str(CHAR_LOOP_MIN - 1) + ",}", re.DOTALL)
#: 区切り (読点・句点・改行) で割った断片がこの数だけ並んだ列が、SEGMENT_REPEAT_MIN 回以上
#: 現れたら暴走 (句点の無い列挙の反復。本番 s17 で社名 22 個の列挙を 12,499 字まで繰り返した)
SEGMENT_RUN = 5
SEGMENT_REPEAT_MIN = 3
_SEGMENT_SPLIT = re.compile(r"[、。,，\n]+")


def _repeated_sentences(text: str) -> int:
    counts = Counter(s.strip() for s in _SENTENCE.findall(text))
    return counts.most_common(1)[0][1] if counts else 0


def _has_char_loop(text: str) -> bool:
    """空白だけの型 (転載本文の字下げ等) は数えない。"""
    return any(m.group(1).strip() for m in _CHAR_LOOP.finditer(text))


def _has_segment_loop(text: str) -> bool:
    segs = [x.strip() for x in _SEGMENT_SPLIT.split(text) if x.strip()]
    runs = Counter(tuple(segs[i : i + SEGMENT_RUN]) for i in range(len(segs) - SEGMENT_RUN + 1))
    return bool(runs) and runs.most_common(1)[0][1] >= SEGMENT_REPEAT_MIN


def _is_runaway_text(text: str) -> bool:
    return (
        _repeated_sentences(text) >= SENTENCE_REPEAT_MIN
        or _has_char_loop(text)
        or _has_segment_loop(text)
    )


def runaway_reasons(summary: SummaryOutput) -> list[str]:
    """暴走の兆候 (欄名と種類)。空なら正常 (純粋関数)。"""
    reasons: list[str] = []
    for name, value in summary.model_dump().items():
        if isinstance(value, str) and _is_runaway_text(value):
            reasons.append(f"{name}:text_repeat")
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


def _cut_segment_loop(text: str) -> str:
    """断片の列が 2 回目に現れる位置で切り詰める (列挙の反復は後ろに情報が無い)。"""
    parts = re.split(r"([、。,，\n]+)", text)  # [断片, 区切り, 断片, 区切り, ...]
    segs = [(i, p.strip()) for i, p in enumerate(parts) if i % 2 == 0 and p.strip()]
    seen: set[tuple[str, ...]] = set()
    for k in range(len(segs) - SEGMENT_RUN + 1):
        run = tuple(x for _, x in segs[k : k + SEGMENT_RUN])
        if run in seen:
            return "".join(parts[: segs[k][0]]).rstrip("、,， \n")
        seen.add(run)
    return text


def repair(summary: SummaryOutput) -> SummaryOutput:
    """繰り返しを畳んだ新しい要約 (元は変更しない)。"""
    update: dict[str, Any] = {}
    for name, value in summary.model_dump().items():
        if isinstance(value, str) and _is_runaway_text(value):
            # 連続する短い型は 1 回に畳み、そのあと同じ文の 2 回目以降を落とす
            text = _collapse_sentences(_CHAR_LOOP.sub(r"\1", value))
            update[name] = _cut_segment_loop(text) if _has_segment_loop(text) else text
        elif isinstance(value, list) and len(value) - len({str(v) for v in value}) >= LIST_DUP_MIN:
            update[name] = list(dict.fromkeys(value))
    return summary.model_copy(update=update) if update else summary
