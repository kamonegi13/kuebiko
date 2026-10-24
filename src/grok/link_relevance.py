"""参照先が投稿の主題に触れているかの関門 (GROK_LINK_RELEVANCE_GATE、2026-10-24)。

GROK_LINK_FETCH の実測 (44 投稿) で、参照先が悪化させたのは「情報の無い参照先」だった
(被害組織自身のトップページ・ダミーテキストのページ・トラッカーの概要ページ)。要約が
「参照先には侵害の記載が無い」を繰り返す・参照先に無い帰属を足す・「参照先 1 は…」と
メタ記述を書く。

これらの参照先が投稿と共有するのは **被害組織の名前だけ** で、事象の語 (攻撃グループ名・
CVE・製品名・マルウェア名) は共有しない。そこで、投稿の固有名詞のうち

- 参照先 URL のホスト名に含まれる語 — 参照先の運営者自身の名前
- 参照先が ``組織名 (https://…`` で名指しされた組織自身のサイトのとき、その組織名の語
- 定型の語 (Ransomware / Alert / Key / 月名 / 国コード / ``-based`` 等)

を除いた **事象の語** が参照先の本文 (と URL の path) に出てくるかを、``claim_grounding`` の
照合の部品 (正規化・カタカナ⇔英字の表記ゆれ) で決定論的に判定する。出てこなければ材料に入れない。
``LinkedPage`` は消さず ``skip_reason`` を付ける (articles.linked_context に本文ごと残す)。

判定の決まり:
- 強い語 = 数字を含む (CVE 等)・語中に大文字がある (ESXi / DeadLock)・全て大文字 (CISA)・
  4 字以上のカタカナ。1 つ共有すれば主題に触れているとみなす
- 弱い語 = 先頭だけ大文字の英単語 (Operation / Care 等)・3 字のカタカナ。一般語と区別が
  つかない。組織自身のサイトでは 2 種類以上共有したときだけ、それ以外のサイトでは 1 つで足りる
  (組織自身のサイトは名前以外の語が偶然一致しやすく、他のサイトは一致自体が証拠になるため)
- 投稿に判定できる語が 1 つも無い (漢字だけの投稿等)・日本語が主で語が 2 つ以下 (組織自身の
  サイトを除く) ときは判定しない (材料に入れる)。ラテン文字の固有名詞の重なりでは測れないため
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, replace
from urllib.parse import urlparse

from src.assessment.claim_grounding import GroundingSources, check_grounding, extract_terms
from src.grok.link_fetch import LinkedPage

#: ``0`` / ``off`` / ``false`` で関門を止める (既定 on)
GATE_ENV = "GROK_LINK_RELEVANCE_GATE"
#: ``LinkedPage.skip_reason`` — 主題に触れない参照先
SKIP_OFF_TOPIC = "off_topic"

#: 組織自身のサイトで、弱い語が主題に触れているとみなすのに必要な共有数
MIN_WEAK_HITS_ON_OWN_SITE = 2
#: 4 字以上のカタカナは強い語として扱う (3 字は一般語と区別しにくい)
_STRONG_KATAKANA_CHARS = 4
#: ``名前 (https://…`` の名前として除く語数の上限 (長い文を丸ごと除かない)
_ANCHOR_MAX_WORDS = 8
#: 参照先のホスト名の一部として除く語の最小長 (``AI`` のような短い語の誤除外を避ける)
_HOST_MIN_TERM_CHARS = 4

_URL = re.compile(r"https?://\S+|\bwww\.\S+")
_ANCHOR = re.compile(r"([^\n(（]*)[(（]\s*((?:https?://|www\.)[^\s)）]*)")
_KATAKANA_ONLY = re.compile(r"[ァ-ヴー]+")
_TITLE_CASE = re.compile(r"[A-Z][a-z]+")
#: ``US-based`` / ``Turkey-based`` のような所在の形容 (事象の語ではない)
_BASED_SUFFIX = re.compile(r"(?i)-based$")
_JAPANESE_CHAR = re.compile(r"[぀-ヿ一-鿿]")
#: 日本語が主の投稿 (漢字・かなの割合) — 固有名詞が少なく、語の重なりでは判定できない
_JAPANESE_MAJORITY_RATIO = 0.4
#: 日本語が主の投稿で、この語数以下なら判定しない (組織自身のサイトを除く)
_MAX_TERMS_UNJUDGEABLE_JA = 2

#: 事象の語にならない定型・汎用の語 (NFKC + 小文字化 + 英数字だけの形で持つ)。
#: 流出サイトの通知 (Ransomware Alert / Key Details / Threat Actor …) の見出し語、文頭の
#: 冠詞や代名詞、月名、国コードなど
_STOP_TERMS: frozenset[str] = frozenset(
    {
        "the", "they", "its", "their", "this", "that", "two", "three", "read", "new",
        "key", "details", "threat", "actor", "actors", "analyst", "analysis", "note",
        "alert", "ransomware", "reported", "report", "data", "compromised", "country",
        "victim", "sector", "update", "breaking", "via", "blog", "news", "group",
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december",
        "usa", "uk", "eu", "cve", "poc", "rce", "apt", "ioc", "iocs",
    }
)  # fmt: skip


@dataclass(frozen=True)
class _Evidence:
    """投稿から取り出した「事象の語」と、参照先が名指しされた組織自身のサイトか。"""

    terms: tuple[str, ...]
    is_own_site: bool


def link_relevance_gate_enabled() -> bool:
    """``GROK_LINK_RELEVANCE_GATE`` (既定 on)。``0`` / ``off`` / ``false`` だけ止める。"""
    return os.environ.get(GATE_ENV, "1").strip().lower() not in {"0", "off", "false"}


def _key(term: str) -> str:
    folded = unicodedata.normalize("NFKC", term).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _host(url: str | None) -> str:
    raw = url or ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return (urlparse(raw).hostname or "").lower()


def _same_site(a: str, b: str) -> bool:
    """ホスト名が同じサイトか (www. の有無とサブドメインの違いは同一とみなす)。"""
    a, b = a.removeprefix("www."), b.removeprefix("www.")
    return bool(a) and bool(b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def _own_site_names(post: str, page: LinkedPage) -> frozenset[str] | None:
    """参照先が ``組織名 (https://…`` で名指しされた組織のサイトなら、その組織名の語のキー。

    別のサイトの記事なら、被害組織の名前は主題に触れている証拠になるので除かない。
    """
    page_hosts = [_host(page.final_url), _host(page.url)]
    keys: set[str] | None = None
    for match in _ANCHOR.finditer(post):
        if not any(_same_site(_host(match.group(2)), h) for h in page_hosts):
            continue
        words = match.group(1).split()[-_ANCHOR_MAX_WORDS:]
        keys = (keys or set()) | {_key(t) for t in extract_terms(" ".join(words))}
    return None if keys is None else frozenset(keys)


def _is_strong(term: str) -> bool:
    if _KATAKANA_ONLY.fullmatch(term):
        return len(term.strip("ー")) >= _STRONG_KATAKANA_CHARS
    return not _TITLE_CASE.fullmatch(term)  # 数字・語中の大文字・全て大文字


def _evidence(post: str, page: LinkedPage) -> _Evidence:
    """投稿の固有名詞から、参照先のホスト名・組織自身の名前・定型の語を除いたもの。"""
    hosts = [_key(_host(page.final_url)), _key(_host(page.url))]
    own_names = _own_site_names(post, page)
    out: list[str] = []
    for term in extract_terms(_URL.sub(" ", post)):
        key = _key(term)
        if not key or key in _STOP_TERMS or _BASED_SUFFIX.search(term):
            continue
        if own_names is not None and key in own_names:
            continue
        if len(key) >= _HOST_MIN_TERM_CHARS and any(key in h for h in hosts):
            continue
        out.append(term)
    return _Evidence(tuple(out), own_names is not None)


def _page_material(page: LinkedPage) -> str:
    """照合の対象: 本文と URL の path (CVE 番号などが path にだけある参照先がある)。"""
    return f"{page.text} {urlparse(page.final_url or page.url).path}"


def _shared(terms: tuple[str, ...], material: str) -> list[str]:
    """``terms`` のうち ``material`` にも出てくる語 (表記ゆれは claim_grounding の照合に従う)。"""
    report = check_grounding(" ".join(terms), GroundingSources(text=material))
    missing = set(report.violations) | set(report.attributions)
    return [t for t in terms if t not in missing]


def _is_unjudgeable_japanese(post: str, evidence: _Evidence) -> bool:
    text = "".join(_URL.sub(" ", post).split())
    if not text or len(evidence.terms) > _MAX_TERMS_UNJUDGEABLE_JA:
        return False
    return len(_JAPANESE_CHAR.findall(text)) / len(text) >= _JAPANESE_MAJORITY_RATIO


def is_on_topic(post: str, page: LinkedPage) -> bool:
    """参照先が投稿の事象の語を共有するか (判定の決まりはモジュールの docstring)。

    投稿に判定できる語が無ければ True (判定しない)。
    """
    evidence = _evidence(post, page)
    if not evidence.terms:
        return True
    shared = _shared(evidence.terms, _page_material(page))
    if any(_is_strong(t) for t in shared):
        return True
    if not evidence.is_own_site and _is_unjudgeable_japanese(post, evidence):
        return True
    needed = MIN_WEAK_HITS_ON_OWN_SITE if evidence.is_own_site else 1
    return len({_key(t) for t in shared}) >= needed


def apply_relevance_gate(post: str, pages: tuple[LinkedPage, ...]) -> tuple[LinkedPage, ...]:
    """主題に触れない参照先に ``skip_reason`` を付ける (本文・ok は残す)。失敗ページは対象外。"""
    return tuple(
        p
        if not (p.ok and p.text) or is_on_topic(post, p)
        else replace(p, skip_reason=SKIP_OFF_TOPIC)
        for p in pages
    )
