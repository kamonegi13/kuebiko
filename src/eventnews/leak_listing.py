"""流出サイトの掲載の形の記事を見分ける (純粋関数、2026-11-21)。

頻出の名前 + 時刻の近さで統合の候補を作ると (``EVENTNEWS_MERGE_FREQUENT_RECENT``)、
「LockBit 5.0、被害組織 6 件を掲載」(複数の被害者の束) と「LockBit: victim-a.example」
(そのうち 1 件の掲載) が、グループ名・時刻・本文の書式が揃うため ML に「同じ」と判定される。
定義 (docs/event_identity_definition.md §1) ではこれは**包含** (束と 1 件) か別の被害者で、
同じ事象ではない。事前登録の標本 54 組で、確率 0.75 以上の誤り 7 件のうち 6 件がこの型だった。

対象は共有する頻出の名前が**すべて**アクター・マルウェアの名前の組だけ (流出サイトの掲載は
グループ名で結ばれる)。被害組織・道具の名前の共有 (Binance・Cairn など) には当てない。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: 流出サイトの掲載の記事 id 接頭辞 (``sources/ransomware_live._article_id`` と同じ)
LEAK_POSTING_ID_PREFIX = "rl_"
#: この型の判定に使う共有の名前の種類
LEAK_NAME_TYPES: frozenset[str] = frozenset({"actor", "malware_family"})
#: 掲載先を名指しする語 (束の見出し「… をリークサイトに掲載」など)
_LISTING = re.compile(r"(リークサイト|ダーク\s*ウェブ|leak\s*site|dark\s*web|data\s*leak)", re.I)
#: 「グループ名: 被害者」の見出し (グループ名は英数字)
_POSTING = re.compile(r"^([A-Za-z0-9][A-Za-z0-9 .$&'_-]{0,40}):\s*\S")
#: グループ名の照合に使う先頭の文字数 (「Akira ransomware」⇔ akira_ransom、「LockBit5」⇔ lockbit)
_NAME_PREFIX = 4


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def is_leak_form(article_id: str, title: str, group_names: Iterable[str]) -> bool:
    """記事が流出サイトの掲載 (1 件) か、その束を報じる形か。"""
    if article_id.startswith(LEAK_POSTING_ID_PREFIX) or _LISTING.search(title or ""):
        return True
    m = _POSTING.match(title or "")
    if not m:
        return False
    head = _norm(m.group(1))[:_NAME_PREFIX]
    return any(
        len(n) >= _NAME_PREFIX and _norm(n)[:_NAME_PREFIX] == head for n in map(_norm, group_names)
    )


def is_leak_listing_pair(
    shared_keys: Iterable[tuple[str, str]],
    a: tuple[str, str],
    b: tuple[str, str],
) -> bool:
    """共有の名前がグループ名だけで、片方が流出サイトの掲載の形なら True。

    ``a`` / ``b`` は (記事 id, 見出し)。
    """
    keys = list(shared_keys)
    if not keys or any(t not in LEAK_NAME_TYPES for t, _ in keys):
        return False
    names = [v for _, v in keys]
    return is_leak_form(a[0], a[1], names) or is_leak_form(b[0], b[1], names)
