"""発表そのものが出来事の記事種別 — basis=reported を disclosed として扱う (2026-10-19)。

当局の発表 (authority)・流出サイトへの掲載 (leak_site)・勧告 (advisory) は、発表した日が
出来事の日なので、報道日の写し (reported) ではなく公表日 (disclosed) とみなしてよい
(利用者判断。s24b 教師の「発表そのものが出来事」と同じ定義)。

種別は本番の列から決定的に判定する (推測しない):
- advisory   = article_type == 'advisory' かつ発行元そのもの (official ティア / vendor_official)
- leak_site  = 流出サイトを巡回する source (feed_title のみ。category は見ない)
- authority  = 当局発の source (classify_source_tier が official かつ ベンダー/当事者でない)
               で、解説・論説・まとめでない記事

env EVENT_DATE_PUBLICATION_AS_EVENT=0 で旧挙動 (reported のまま)。
"""

from __future__ import annotations

import os
from typing import Literal

PublicationKind = Literal["authority", "leak_site", "advisory"]

FLAG_ENV = "EVENT_DATE_PUBLICATION_AS_EVENT"
# 流出サイトの掲載を取り込む source (feed_title、小文字比較)
LEAK_SITE_FEED_TITLES = frozenset({"ransomware.live", "distributed denial of secrets"})
ADVISORY_ARTICLE_TYPE = "advisory"
# 当局発でも発表そのものとは言えない記事の型
NON_PUBLICATION_ARTICLE_TYPES = frozenset({"opinion", "tutorial", "research", "recap"})
NON_AUTHORITY_ACCOUNT_CLASSES = frozenset({"vendor_official", "affected_party"})


def is_enabled() -> bool:
    return os.environ.get(FLAG_ENV, "1").strip().lower() not in {"0", "false", "off", "no"}


def _source_tier(feed_title: str, feed_url: str, account_class: str) -> str:
    if not (feed_title or feed_url or account_class):
        return "unknown"
    from src.cti.source_basis import classify_source_tier

    return classify_source_tier(feed_title, feed_url, account_class=account_class)


def publication_kind(
    *,
    article_type: str | None = None,
    category: str | None = None,
    feed_title: str | None = None,
    feed_url: str | None = None,
    account_class: str | None = None,
) -> PublicationKind | None:
    """発表そのものが出来事の種別。当たらなければ None。

    解説・論説・まとめ・研究は 3 種別すべてで除外する。advisory は発行元そのもの
    (当局または vendor_official) が出したときだけ (報道媒体が勧告を報じた記事は
    報道日 != 勧告日のため対象外)。leak_site は feed_title だけで判定する。
    """
    atype = (article_type or "").strip()
    if atype in NON_PUBLICATION_ARTICLE_TYPES:
        return None
    title = (feed_title or "").strip()
    url = (feed_url or "").strip()
    acls = (account_class or "").strip()
    if title.lower() in LEAK_SITE_FEED_TITLES:
        return "leak_site"
    tier = _source_tier(title, url, acls)
    if atype == ADVISORY_ARTICLE_TYPE:
        is_issuer = tier == "official" and acls != "affected_party"
        return "advisory" if is_issuer else None
    if acls in NON_AUTHORITY_ACCOUNT_CLASSES:
        return None
    if acls == "gov_official" or tier == "official":
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
