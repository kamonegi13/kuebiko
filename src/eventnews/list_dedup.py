"""事象ニュース draft のリスト欄の重複行畳み込み (2026-09-11、決定論関門)。

背景: レシピ是正した SFT モデル (n17 Stage 1) が Q4・温度 0.2 で反復ループを出した
(discrepancies 36 件中 32 件が同文 / unknowns 335 件中 326 件が同文)。教師 (Opus) の
リスト欄に重複は 0 件で、重複は常に契約違反。指示では止まらない種類の欠陥なので、
書込前に **同文を最初の 1 件に畳む** (順序は保持、先頭を残す)。

正規化は空白の畳み込みのみ (意味の近さで潰さない — 近い別主張を消す誤りの方が重い)。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from src.eventnews.models import EventNewsDraft, FactItem

LIST_FIELDS: tuple[str, ...] = ("key_points", "facts", "discrepancies", "caveats", "unknowns")


def _key(item: str | FactItem) -> str:
    text = item.text if isinstance(item, FactItem) else item
    return " ".join(text.split())


def dedup_items[T: (str, FactItem)](items: Sequence[T]) -> list[T]:
    """同文 (空白正規化後) を先頭 1 件に畳む。順序は保持。"""
    seen: set[str] = set()
    kept: list[T] = []
    for item in items:
        key = _key(item)
        if key in seen:
            continue
        seen.add(key)
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
