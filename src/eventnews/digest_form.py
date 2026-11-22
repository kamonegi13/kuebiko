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


#: 本文の冒頭のニュースレターの定型文 (2026-11-22)。Foreign Policy の World Brief は
#: 題が筆頭の話題で、本文の冒頭に「Welcome back to World Brief, where we're looking at …」とある。
#: 過去 30 日の本文で当たるのは World Brief 14・週次のまとめ 1・四半期のまとめ 1
#: (「in this issue」は普通の記事に当たるので外した)
_DIGEST_BODY = re.compile(
    r"(welcome (back )?to (the )?[a-z' ]{0,30}(brief|newsletter|digest|roundup|recap)\b|"
    r"in (today's|this week's) (newsletter|edition|issue|brief)\b|in this (newsletter|edition)\b|"
    r"this week in [a-z ]{0,20}(security|cyber)|you're reading [a-z' ]{0,30}(newsletter|brief))",
    re.IGNORECASE,
)
#: 本文の冒頭として見る字数
BODY_HEAD_CHARS = 1500


def is_digest_article(title: str, original_title: str, url: str, body: str = "") -> bool:
    """日報・アラート・まとめの形の記事か (題・原題・URL・本文の冒頭のどれかに印)。"""
    if body and _DIGEST_BODY.search(body[:BODY_HEAD_CHARS].replace("\u2019", "'")):
        return True
    for t in (title, original_title):
        if not t:
            continue
        if _DIGEST_TITLE.search(t) or roundup_basis(t) == "title":
            return True
    return bool(url and _DIGEST_URL.search(url))


#: 種別の上書き (``EVENT_KIND_DIGEST_OVERRIDE``) 用の強い題の印。裸の「日報」(朝鮮日報など)・
#: まとめの題の判定 (``roundup_basis``: 「Weekly Downloads」「第8回」「this week」に
#: 当たる) は使わない
_NEWSLETTER_TITLE = re.compile(
    r"(【[^】]{0,8}(日報|日报|週報|周报)】|daily\s+(brief|briefing|digest)|world\s+brief|"
    r"morning\s+brief|evening\s+brief)",
    re.IGNORECASE,
)


def is_newsletter_article(title: str, original_title: str, url: str, body: str = "") -> bool:
    """日報・ニュースレターの強い印 (本文冒頭の定型文・原題の【…日報】等) があるか。

    ``is_digest_article`` より狭い (種別を roundup に上書きし、群化・統合のすべてに効くため)。
    URL の印は使わない: Silobreaker の日次アラートは 1 話題の回もあり (Opus の判定で両方あった)、
    URL だけで roundup にすると単独の話題の記事まで群化から外れる。``url`` は署名の互換のため。
    """
    if body and _DIGEST_BODY.search(body[:BODY_HEAD_CHARS].replace("’", "'")):
        return True
    del url
    return any(bool(t and _NEWSLETTER_TITLE.search(t)) for t in (title, original_title))
