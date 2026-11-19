"""攻撃者側の主張 (リークサイト掲載・犯行声明) の記事の判定。

発生の確度の根拠 (被害組織の公表・政府の確認) に数えない。攻撃者が自分で掲載・声明した
ものは「事案があった」ことの確認ではなく主張であるため。既存の流出サイト source の判定
(``cti.publication_event.LEAK_SITE_FEED_TITLES``) を再利用し、題の語を足す。
"""

from __future__ import annotations

import re

from src.cti.publication_event import LEAK_SITE_FEED_TITLES

_TITLE_RE = re.compile(
    r"リークサイト|流出サイト|犯行声明|犯行を主張|犯行を認め|攻撃を主張|被害者として掲載|に掲載|"
    r"leak\s*site|claims?\s+(?:responsibility|to\s+have|the\s+attack)|claimed\s+(?:responsibility|the\s+attack)|"
    r"(?:listed|posted|added)\s+(?:on|to)\s+(?:its\s+|their\s+|the\s+)?(?:\w+\s+)?(?:leak|extortion|dark\s*web)",
    re.IGNORECASE,
)


def is_attacker_claim(title: str, feed_title: str = "") -> bool:
    """題または source が攻撃者側の主張 (リークサイト掲載・犯行声明) を示すか。"""
    if feed_title.strip().lower() in LEAK_SITE_FEED_TITLES:
        return True
    return bool(_TITLE_RE.search(title))
