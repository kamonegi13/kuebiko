"""発表そのものが出来事の記事種別 — basis=reported を disclosed として扱う (2026-10-19)。

当局の発表 (authority)・流出サイトへの掲載 (leak_site)・勧告 (advisory) は、発表した日が
出来事の日なので、報道日の写し (reported) ではなく公表日 (disclosed) とみなしてよい
(利用者判断。s24b 教師の「発表そのものが出来事」と同じ定義)。

種別は本番の列から決定的に判定する (推測しない):
- advisory   = articles.article_type == 'advisory'
- leak_site  = category == 'apt_leak' または流出サイトを巡回する source (feed_title)
- authority  = 当局発の source (classify_source_tier が official かつ ベンダー/当事者でない)
               で、解説・論説・まとめでない記事

env EVENT_DATE_PUBLICATION_AS_EVENT=0 で旧挙動 (reported のまま)。
"""

from __future__ import annotations

import os
from typing import Literal

PublicationKind = Literal["authority", "leak_site", "advisory"]

FLAG_ENV = "EVENT_DATE_PUBLICATION_AS_EVENT"
LEAK_SITE_CATEGORIES = frozenset({"apt_leak"})
# 流出サイトの掲載を取り込む source (feed_title、小文字比較)
LEAK_SITE_FEED_TITLES = frozenset({"ransomware.live", "distributed denial of secrets"})
ADVISORY_ARTICLE_TYPE = "advisory"
# 当局発でも発表そのものとは言えない記事の型
NON_PUBLICATION_ARTICLE_TYPES = frozenset({"opinion", "tutorial", "research", "recap"})
NON_AUTHORITY_ACCOUNT_CLASSES = frozenset({"vendor_official", "affected_party"})


def is_enabled() -> bool:
    return os.environ.get(FLAG_ENV, "1").strip().lower() not in {"0", "false", "off", "no"}


def _is_authority(article_type: str, feed_title: str, feed_url: str, account_class: str) -> bool:
    if article_type in NON_PUBLICATION_ARTICLE_TYPES:
        return False
    if account_class in NON_AUTHORITY_ACCOUNT_CLASSES:
        return False
    if account_class == "gov_official":
        return True
    if not (feed_title or feed_url):
        return False
    from src.cti.source_basis import classify_source_tier

    return classify_source_tier(feed_title, feed_url) == "official"


def publication_kind(
    *,
    article_type: str | None = None,
    category: str | None = None,
    feed_title: str | None = None,
    feed_url: str | None = None,
    account_class: str | None = None,
) -> PublicationKind | None:
    """発表そのものが出来事の種別。当たらなければ None。"""
    atype = (article_type or "").strip()
    title = (feed_title or "").strip()
    if (category or "") in LEAK_SITE_CATEGORIES or title.lower() in LEAK_SITE_FEED_TITLES:
        return "leak_site"
    if atype == ADVISORY_ARTICLE_TYPE:
        return "advisory"
    if _is_authority(atype, title, (feed_url or "").strip(), (account_class or "").strip()):
        return "authority"
    return None


def resolve_publication_basis(
    basis: str | None,
    *,
    article_type: str | None = None,
    category: str | None = None,
    feed_title: str | None = None,
    feed_url: str | None = None,
    account_class: str | None = None,
) -> str | None:
    """basis=reported かつ発表の種別なら disclosed、それ以外は basis をそのまま返す。"""
    if basis != "reported" or not is_enabled():
        return basis
    kind = publication_kind(
        article_type=article_type,
        category=category,
        feed_title=feed_title,
        feed_url=feed_url,
        account_class=account_class,
    )
    return "disclosed" if kind else basis
