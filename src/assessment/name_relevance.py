"""記事・塊の固有の名前が、状況の題・最新の判定文に出るかの共通規則 (台帳の上流の修正 §8、2 回目)。

SEED_STRICT・ASSIGN_STRONG_ANCHOR・DUP_RESCUE が同じ規則を使う (純粋関数のみ)。名前の出どころ:
article_entities の victim_org / affected_product / malware_family / campaign / actor /
actor_provisional / cve の値と、題の固有語 (英数字 3 字以上・カタカナ 3 字以上・漢字 4 字以上)。
照合は正規化 (NFKC・casefold・空白と記号の除去・組織の法人格の除去) した部分文字列で行う。
国名・一般語は除外し、名前の最小長は ``MIN_NAME_LEN``。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable
from functools import lru_cache

#: 名前の最小の長さ (正規化後)
MIN_NAME_LEN = 3
#: 名前として数える entity の型
NAME_ENTITY_TYPES: frozenset[str] = frozenset(
    {
        "victim_org",
        "affected_product",
        "malware_family",
        "campaign",
        "actor",
        "actor_provisional",
        "cve",
    }
)

#: 弁別力の低い一般語 (日本語 + 英語、小文字)。侵害の報道に毎回出る語・題の定型語
GENERIC_WORDS: frozenset[str] = frozenset(
    {
        "不正アクセス", "不正", "アクセス", "情報", "漏洩", "漏えい", "流出", "個人情報", "公表",
        "発表", "攻撃", "被害", "可能性", "顧客", "侵害", "サイバー", "セキュリティ", "システム",
        "サーバ", "サーバー", "サービス", "お知らせ", "お詫び", "調査", "対応", "影響", "報告",
        "脆弱性", "ランサムウェア", "マルウェア", "ウイルス", "インシデント", "ニュース", "対策",
        "data", "breach", "hack", "hacked", "leak", "leaked", "attack", "attacks", "cyber",
        "security", "system", "systems", "incident", "access", "unauthorized", "customer",
        "customers", "information", "personal", "report", "reports", "company", "users",
        "ransomware", "malware", "vulnerability", "exploited", "critical", "update", "patch",
        "researchers", "hackers", "group", "new", "the", "and", "for", "with", "from", "says",
        "after", "over", "that", "this", "into", "about", "more", "than",
    }
)  # fmt: skip

_CORP_RE = re.compile(
    r"株式会社|\(株\)|有限会社|合同会社|一般社団法人|一般財団法人|"
    r"\b(?:inc|ltd|llc|corp|corporation|co|gmbh|limited|plc)\b\.?",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[\W_]+")
_TITLE_NAME_RE = re.compile(r"[0-9a-z]{3,}|[ァ-ヶー]{3,}|[一-龠]{4,}")


def normalize_name(text: str) -> str:
    """NFKC・casefold・法人格の除去・空白と記号の除去。"""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = _CORP_RE.sub("", t)
    return _PUNCT_RE.sub("", t)


@lru_cache(maxsize=1)
def _blocklist() -> frozenset[str]:
    from src.assessment.assignment import _nation_name_tokens  # noqa: PLC0415 — 循環 import 回避

    return frozenset(normalize_name(w) for w in GENERIC_WORDS) | frozenset(
        normalize_name(w) for w in _nation_name_tokens()
    )


@lru_cache(maxsize=1)
def _cjk_generic_words() -> tuple[str, ...]:
    """複合の一般語 (情報漏洩・不正アクセス被害 等) を見抜くための CJK の一般語 (長い順)。"""
    words = {normalize_name(w) for w in GENERIC_WORDS if not w.isascii()}
    return tuple(sorted(words, key=len, reverse=True))


def _is_generic_compound(name: str) -> bool:
    """CJK の一般語だけを連ねた語 (除くと固有の部分が MIN_NAME_LEN 未満) か。"""
    if name.isascii():
        return False
    rest = name
    for w in _cjk_generic_words():
        rest = rest.replace(w, "")
    return len(rest) < MIN_NAME_LEN


def _usable(name: str) -> bool:
    return (
        len(name) >= MIN_NAME_LEN
        and not name.isdigit()  # 数字だけは名前でない (件数・番号)
        and name not in _blocklist()
        and not _is_generic_compound(name)
    )


def title_names(title: str) -> frozenset[str]:
    """題の固有語 (正規化済み)。ひらがなだけの語は拾わない。"""
    t = unicodedata.normalize("NFKC", title or "").casefold()
    return frozenset(
        n for n in (normalize_name(m) for m in _TITLE_NAME_RE.findall(t)) if _usable(n)
    )


def entity_names(entity_keys: Iterable[str]) -> frozenset[str]:
    """ "type:value" のうち名前の型の値 (正規化済み)。"""
    out: set[str] = set()
    for k in entity_keys:
        etype, _, value = k.partition(":")
        if etype in NAME_ENTITY_TYPES:
            n = normalize_name(value)
            if _usable(n):
                out.add(n)
    return frozenset(out)


def article_names(title: str, entity_keys: Iterable[str]) -> frozenset[str]:
    """記事の固有の名前 = entity の名前 + 題の固有語。"""
    return entity_names(entity_keys) | title_names(title)


def matched_names(names: Collection[str], text: str) -> tuple[str, ...]:
    """``names`` (正規化済み) のうち ``text`` に出るもの。他の一致の部分文字列は数えない。"""
    haystack = normalize_name(text)
    hits = sorted(n for n in names if n in haystack)
    return tuple(n for n in hits if not any(n != m and n in m for m in hits))


def is_relevant(names: Collection[str], text: str) -> bool:
    """名前が 1 つでも ``text`` (状況の題 + 最新の判定文) に出れば関係あり。"""
    haystack = normalize_name(text)
    return any(n in haystack for n in names)
