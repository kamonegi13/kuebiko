"""記事の出典ドメインが IOC として保存されるのを決定論で遮断する SSoT (2026-08-19)。

判定基準は「出典系の URL (記事のリンク、ベンダーブログ、参考文献) は **IOC ではない**。
含めないこと」「『○○社が報告した』レベルの引用先 URL を含めない」と明記している。
それでも実測 (全期間の ioc_domain / ioc_url 3,403 件) で:

- **44 件** が購読ソースのドメイン (twz.com 20 / justsecurity.org 9 / wiz.io 3 等)
- **32 件** が **その記事自身の出典ホスト**

IOC は「照合に使う値」なので、報道サイトが混ざると突合の noise になり、同一 infra を
根拠にした記事の関連付けが偽の辺を作る。

⭐ **指示では止まらない** (2026-08-19 に判明した 5 例と同型) ため決定論の関門で止める。

⚠ 対象は host を持つ IOC (ioc_domain / ioc_url) のみ。IP / hash は出典と混同しない。
⚠ victim_org の PROTECTED_CATEGORIES のような例外は置かない — IOC は「攻撃に使われた値」
   であり、そのソース自身が侵害された記事でも出典ホストは IOC ではない (被害の記録は
   victim_org が担う)。
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from src.logging_config import get_logger

_log = get_logger(__name__)

_URL_IN_TEXT = re.compile(r"https?://[^\s\"'\\<>]+")
_TRANSPORTS = ("rss", "sitemap", "html_scraper")

# プロセス内 1 回だけ構築する。pipeline は run ごとに subprocess なのでキャッシュは
# run をまたがず、ソース追加の反映漏れは起きない。
_SOURCE_HOSTS: frozenset[str] | None = None


def normalize_host(value: str) -> str:
    """IOC 値 (URL / 素のドメイン) から比較用の host を取り出す。取れなければ空文字。"""
    raw = value.strip()
    if not raw:
        return ""
    host = (urlparse(raw).hostname or "") if "://" in raw else raw.split("/")[0]
    host = host.strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _hosts_from_entries(entries: list[dict[str, Any]]) -> set[str]:
    """ソース定義 (transport ごとに key が違う) から host を総取りする。

    ``url`` / ``sitemap`` / ``site`` と key 名が transport で割れているため、entry を
    JSON 化して URL を総なめする。key 名の差分をここに複製しないための割り切り。
    """
    hosts: set[str] = set()
    for url in _URL_IN_TEXT.findall(json.dumps(entries, ensure_ascii=False)):
        host = normalize_host(url)
        if host:
            hosts.add(host)
    return hosts


def source_hosts() -> frozenset[str]:
    """購読中の全ソース (rss / sitemap / html_scraper) の host 集合。

    読めなければ空集合を返す (fail-open) — 出典 filter が落ちても取込は止めない。
    自記事ホストの判定は registry に依存しないので、その分だけは常に効く。
    """
    global _SOURCE_HOSTS  # noqa: PLW0603
    if _SOURCE_HOSTS is not None:
        return _SOURCE_HOSTS
    hosts: set[str] = set()
    try:
        from src.sources.source_store import load_entries

        for transport in _TRANSPORTS:
            hosts |= _hosts_from_entries(load_entries(transport))  # type: ignore[arg-type]
    except Exception as exc:  # 設定破損 / DB 障害: filter を諦めて取込は続ける
        _log.warning("ioc_source_hosts_unavailable", error=type(exc).__name__)
    _SOURCE_HOSTS = frozenset(hosts)
    return _SOURCE_HOSTS


def is_source_reference(
    value: str, *, article_url: str | None = None, hosts: frozenset[str] | None = None
) -> bool:
    """``value`` (ioc_domain / ioc_url) が出典側のドメインか (純粋判定)。

    ``hosts`` を渡さなければ購読ソース一覧を使う (テストは明示的に渡す)。
    """
    host = normalize_host(value)
    if not host:
        return False
    if article_url and host == normalize_host(article_url):
        return True
    known = source_hosts() if hosts is None else hosts
    return host in known
