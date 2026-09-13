"""事象ニュース draft のリスト欄の重複行畳み込み (2026-09-11、決定論関門)。

背景: レシピ是正した SFT モデル (n17 Stage 1) が Q4・温度 0.2 で反復ループを出した
(discrepancies 36 件中 32 件が同文 / unknowns 335 件中 326 件が同文)。教師 (Opus) の
リスト欄に重複は 0 件で、重複は常に契約違反。指示では止まらない種類の欠陥なので、
書込前に **同文を最初の 1 件に畳む** (順序は保持、先頭を残す)。

正規化は空白の畳み込みのみ (意味の近さで潰さない — 近い別主張を消す誤りの方が重い)。
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from typing import Any

from src.eventnews.models import EventNewsDraft, FactItem

LIST_FIELDS: tuple[str, ...] = ("key_points", "facts", "discrepancies", "caveats", "unknowns")
# 言い換えだけの近傍重複 (「〜と記載している」/「〜と記載」等) を畳む類似度の下限。
# 実測 (2026-09-13、凍結 39 件): 教師と N1 は 0.8 以上の対が 0、是正レシピの生徒は 2/39 item。
# 0.8 未満は別主張とみなす (近い別主張を消す誤りの方が重い)。
NEAR_DUP_RATIO = 0.85
# 近傍重複の判定は長文の言い換えに限る。短い主張は 1 語の差が意味差 (「80 組織」/「80 組織超」)。
NEAR_DUP_MIN_CHARS = 40


def _key(item: str | FactItem) -> str:
    text = item.text if isinstance(item, FactItem) else item
    return " ".join(text.split())


def _diff_has_digit(a: str, b: str) -> bool:
    """差分側に数字が含まれるか (数値・日付・版数の違いは別主張とみなす)。"""
    sm = difflib.SequenceMatcher(None, a, b)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal" and any(ch.isdigit() for ch in a[i1:i2] + b[j1:j2]):
            return True
    return False


def _is_near_dup(key: str, kept_keys: Sequence[str]) -> bool:
    if len(key) < NEAR_DUP_MIN_CHARS:
        return False
    for other in kept_keys:
        if len(other) < NEAR_DUP_MIN_CHARS:
            continue
        if difflib.SequenceMatcher(None, key, other).ratio() < NEAR_DUP_RATIO:
            continue
        if _diff_has_digit(key, other):
            continue
        return True
    return False


def dedup_items[T: (str, FactItem)](items: Sequence[T]) -> list[T]:
    """同文と近傍重複 (長文・類似度 NEAR_DUP_RATIO 以上・数字差なし) を先頭 1 件に畳む。"""
    kept_keys: list[str] = []
    kept: list[T] = []
    for item in items:
        key = _key(item)
        if key in kept_keys or _is_near_dup(key, kept_keys):
            continue
        kept_keys.append(key)
        kept.append(item)
    return kept


def dedup_draft(draft: EventNewsDraft) -> tuple[EventNewsDraft, dict[str, int]]:
    """全リスト欄を畳み、(新 draft, 欄ごとの除去数) を返す。除去がなければ同一オブジェクト。"""
    removed: dict[str, int] = {}
    update: dict[str, list[Any]] = {}
    for field in LIST_FIELDS:
        original: list[Any] = list(getattr(draft, field))
        kept = dedup_items(original)
        if len(kept) != len(original):
            removed[field] = len(original) - len(kept)
            update[field] = kept
    if not update:
        return draft, removed
    return draft.model_copy(update=update), removed
