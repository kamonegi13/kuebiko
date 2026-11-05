"""DETECT-JUDGE の R18 (閉包による汚染除外、prereg §7) の純粋部分。"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Protocol

FOCUS_CVE_MAX = 3
MAX_COMPONENT_SHARE = 0.30
COS_THRESHOLD = 0.80

_ID_TOKEN = re.compile(r"[A-Za-z0-9_\-]+:[^\s\"'\\<>\[\],]+")
_TRAILING = ".;)"


class _UnitLike(Protocol):
    @property
    def article_ids(self) -> tuple[str, ...]: ...

    @property
    def stratum(self) -> str: ...


def apply_hub_cut(
    values: Mapping[str, frozenset[str]], cap: int = FOCUS_CVE_MAX
) -> dict[str, frozenset[str]]:
    """欄の値が cap を超える記事は、その欄を鍵にしない (空の欄も鍵にしない)。"""
    return {a: v for a, v in values.items() if 0 < len(v) <= cap}


def id_tokens(text: str) -> set[str]:
    """テキストから記事 id らしい語を取る (DB で実在を確かめる前の候補。末尾の句読点は両方残す)。"""
    out: set[str] = set()
    for m in _ID_TOKEN.finditer(text):
        tok = m.group(0)
        out.add(tok)
        stripped = tok.rstrip(_TRAILING)
        if stripped != tok:
            out.add(stripped)
    return out


def contaminated_units(
    units: Mapping[str, _UnitLike], component_of: Mapping[str, str], train_ids: frozenset[str]
) -> set[str]:
    """単位の記事のどれかが、学習側の記事と同じ成分に入る単位 (成分は割らない)。"""
    train_components = {component_of.get(a, a) for a in train_ids}
    return {
        uid
        for uid, u in units.items()
        if any(component_of.get(a, a) in train_components for a in u.article_ids)
    }


def count_by_stratum(
    units: Mapping[str, _UnitLike], uids: Sequence[str] | set[str]
) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for uid in uids:
        out[units[uid].stratum] += 1
    return dict(out)
