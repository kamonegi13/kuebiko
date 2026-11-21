"""複数の話題を並べた日報・アラートの記事を見分ける (純粋関数、2026-11-21)。

まとめ記事の判定 (``MemberArticle.is_roundup``) は LLM が付けた記事の種別 (roundup) だけを見る。
日報の類 (iThome の【資安日報】・Silobreaker の日次アラート・Foreign Policy の World Brief 等) は
題が筆頭の事案の見出しなので、種別は exploitation / breach と付き、判定をすり抜ける。
さらに和訳した題では「【資安日報】」の接頭辞が落ちる。頻出の名前 + 時刻の近さで統合の候補を
作ると、筆頭の事案の記事と「同じ」に見えて誤統合になる
(事前登録 merge_frequent_recent の開発用 84 組で、包含の誤りの主な型)。

原題・和訳の題・URL のどれかに日報の印があれば日報とみなす。
"""

from __future__ import annotations

import re

from src.cti.roundup_detector import roundup_basis

#: 題 (原題・和訳) の日報の印
_DIGEST_TITLE = re.compile(
    r"(【[^】]{0,8}(日報|日报|週報|周报)】|資安日報|资安日报|日報|daily\s+(brief|briefing|digest|alert)|"
    r"world\s+brief|morning\s+brief|evening\s+brief|cyber\s+alert)",
    re.IGNORECASE,
)
#: URL の日報の印 (日次のアラート・ブリーフの連載)
_DIGEST_URL = re.compile(
    r"(silobreaker-cyber-alert|/world-brief|/daily-brief|/morning-brief|/newsletters?/)",
    re.IGNORECASE,
)


def is_digest_article(title: str, original_title: str, url: str) -> bool:
    """日報・アラート・まとめの形の記事か (題・原題・URL のどれかに印)。"""
    for t in (title, original_title):
        if not t:
            continue
        if _DIGEST_TITLE.search(t) or roundup_basis(t) == "title":
            return True
    return bool(url and _DIGEST_URL.search(url))
