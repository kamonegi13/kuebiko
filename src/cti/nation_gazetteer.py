"""タイトル/claim テキストからの決定論的な国名導出 (Situation Ledger 段A)。

**抽出ギャップ対策**: entity_type='involved_country' は per-article LLM 要約の
involved_countries フィールド頼みで、地政学・軍事記事の 4 割超で未抽出
(実測 2026-07-03: プール 1162 件中 no-anchor 490 件)。国名はタイトルに明示される
ことが多いため、config/cti/countries.yaml の alias 辞書 (SSoT) を gazetteer として
テキスト走査し、割当キー (nation anchor) を LLM なしで補完する。

article_entities には書き戻さない (LLM 抽出の provenance を汚さない)。
導出値は Situation の anchors / 割当判定にのみ使う。
設計: docs/synthesis_situation_ledger_design.md §3.1。
"""

from __future__ import annotations

import re
from functools import lru_cache

from src.cti.taxonomy_normalizer import load_normalizer

# ASCII alias はこの長さ以上のみ単語境界マッチ (ISO2/3 コードの誤爆防止)。
_MIN_ASCII_ALIAS_LEN = 4
# 短い ASCII コードで安全に使えるもの (大文字・単語境界の完全一致のみ)。
_SHORT_CODE_WHITELIST: dict[str, str] = {
    "US": "US",
    "U.S.": "US",
    "USA": "US",
    "UK": "GB",
    "PRC": "CN",
    "DPRK": "KP",
    "ROK": "KR",
    "UAE": "AE",
}
# 日本語見出しに頻出する複合語・略記 (countries.yaml alias に無い形)。
# 2 字国名ペア (米中/露宇 等) は見出しの定型で弁別力が高い。
_JP_SUPPLEMENT: dict[str, tuple[str, ...]] = {
    "米軍": ("US",),
    "米国防": ("US",),
    "米空軍": ("US",),
    "米海軍": ("US",),
    "米陸軍": ("US",),
    "米政府": ("US",),
    "米大統領": ("US",),
    "米議会": ("US",),
    "米中": ("US", "CN"),
    "米露": ("US", "RU"),
    "米朝": ("US", "KP"),
    "米韓": ("US", "KR"),
    "日米": ("JP", "US"),
    "中露": ("CN", "RU"),
    "露宇": ("RU", "UA"),
}
# 漢字 alias の直後にこの文字が続く場合は国への言及でない (日本語/中国語 等)。
_KANJI_FALSE_SUFFIX = "語"
# 国名 alias を内包するが国への言及でない複合語 (走査前にテキストから除去する)。
# 実測の系統的偽陽性: 「インド太平洋」(地域概念) → IN。カタカナ境界規則は後続の漢字で通過するため。
_PRE_REMOVE: tuple[str, ...] = ("インド太平洋", "Indo-Pacific")

_KATAKANA_CLASS = "ァ-ヶー"


def _compile_alias(alias: str) -> re.Pattern[str] | None:
    """alias 1 件を安全な境界規則付き regex にする (危険な形は None で捨てる)。"""
    a = alias.strip()
    if not a:
        return None
    if a.isascii():
        # ISO2/3 等の短コードは whitelist (呼出側) に任せ、ここでは捨てる
        if len(a) < _MIN_ASCII_ALIAS_LEN:
            return None
        return re.compile(rf"\b{re.escape(a)}\b", re.IGNORECASE)
    if re.fullmatch(rf"[{_KATAKANA_CLASS}]+", a):
        # 純カタカナ (ロシア/タイ/インド...): カタカナ run の内部一致を弾く
        # (タイトル≠タイ、インドネシア≠インド)
        return re.compile(rf"(?<![{_KATAKANA_CLASS}]){re.escape(a)}(?![{_KATAKANA_CLASS}])")
    # 漢字を含む alias (日本/中国/北朝鮮...): 複合語 (中国政府) を拾うため境界は付けず、
    # 「〜語」(日本語/中国語) のみ除外する
    return re.compile(rf"{re.escape(a)}(?!{_KANJI_FALSE_SUFFIX})")


@lru_cache(maxsize=1)
def _patterns() -> tuple[tuple[re.Pattern[str], str], ...]:
    """countries.yaml (SSoT) の alias 辞書から (pattern, iso) 一覧を構築 (process-life cache)。"""
    normalizer = load_normalizer()
    out: list[tuple[re.Pattern[str], str]] = []
    for alias, iso in normalizer.country_lookup.items():
        pat = _compile_alias(alias)
        if pat is not None:
            out.append((pat, iso))
    for word, isos in _JP_SUPPLEMENT.items():
        for iso in isos:
            out.append((re.compile(re.escape(word)), iso))
    return tuple(out)


_SHORT_CODE_RE = re.compile(
    r"(?<![A-Za-z.])(" + "|".join(re.escape(c) for c in _SHORT_CODE_WHITELIST) + r")(?![A-Za-z.])"
)


def nations_in_text(text: str) -> frozenset[str]:
    """テキストに明示された国を ISO 3166-1 alpha-2 の集合で返す (決定論・LLM 不使用)。"""
    if not text:
        return frozenset()
    for phrase in _PRE_REMOVE:
        text = text.replace(phrase, " ")
    found: set[str] = set()
    for pat, iso in _patterns():
        if pat.search(text):
            found.add(iso)
    for m in _SHORT_CODE_RE.finditer(text):
        found.add(_SHORT_CODE_WHITELIST[m.group(1)])
    return frozenset(found)
