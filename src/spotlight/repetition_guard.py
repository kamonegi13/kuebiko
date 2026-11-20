"""Spotlight の同じ文の繰り返しの関門 (SPOTLIGHT_REPETITION_GUARD)。

2026-11-17 の n20rd の多段 Spotlight で、見通しの 1 文が 151 回繰り返された
(1 行の中なので行の重複検査では見つからない)。要約の関門 (``summary_repetition``) と
同じ文区切り・回数の閾値で検出し、**2 回目以降の同じ文だけを取り除く**。
要約の関門は初出の直後で切るが、Spotlight は後ろに (c) の推奨など別の節が続くため切らない。
文面はログに出さない。
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Literal

from src.logging_config import get_logger
from src.pipeline.summary_repetition import SENTENCE_MIN_CHARS, SENTENCE_REPEAT_MIN
from src.tools.sentences import SENTENCE_PATTERN

_log = get_logger(__name__)

GUARD_ENV = "SPOTLIGHT_REPETITION_GUARD"
Mode = Literal["0", "shadow", "1"]


def guard_mode() -> Mode:
    """``SPOTLIGHT_REPETITION_GUARD`` (コードの既定 shadow)。未知の値は shadow。"""
    raw = os.environ.get(GUARD_ENV, "shadow").strip().lower()
    if raw in ("0", "off"):
        return "0"
    if raw in ("1", "on"):
        return "1"
    return "shadow"


def _key(sentence: str) -> str:
    key = sentence.strip()
    return key if len(key) >= SENTENCE_MIN_CHARS else ""


def dedupe_repeated_sentences(text: str) -> tuple[str, int]:
    """閾値以上繰り返された文の 2 回目以降を取り除いた文と、取り除いた数 (純粋関数)。"""
    spans = [(m.start(), m.end(), _key(m.group(0))) for m in SENTENCE_PATTERN.finditer(text)]
    counts = Counter(k for _, _, k in spans if k)
    repeated = {k for k, n in counts.items() if n >= SENTENCE_REPEAT_MIN}
    if not repeated:
        return text, 0
    seen: set[str] = set()
    parts: list[str] = []
    pos = 0
    removed = 0
    for start, end, key in spans:
        if key in repeated and key in seen:
            parts.append(text[pos:start])
            pos = end
            removed += 1
            continue
        seen.add(key)
    parts.append(text[pos:])
    return "".join(parts), removed


def guard_spotlight_text(text: str, *, field: str, pir_id: str) -> str:
    """旗に従う。1 = 取り除く / shadow = ログだけ / 0 = 何もしない。"""
    mode = guard_mode()
    if mode == "0" or not text:
        return text
    fixed, removed = dedupe_repeated_sentences(text)
    if removed == 0:
        return text
    _log.warning(
        "spotlight_repetition",
        pir_id=pir_id,
        field=field,
        removed=removed,
        chars_before=len(text),
        chars_after=len(fixed),
        mode=mode,
    )
    return fixed if mode == "1" else text
