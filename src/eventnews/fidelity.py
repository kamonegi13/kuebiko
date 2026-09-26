"""要約の固有情報の網羅率 — LLM を使わない簡易な正しさの指標 (2026-09-26)。

事象ニュースが元記事の **訳しても変わらない固有情報** (CVE・版番号・数値・ラテン文字の固有名) を
どれだけ運んでいるかを文字列照合で数える。n17c で問題になった「国名・業種・CVE が潰れる」
種類の欠落を、追加の LLM 呼出なしで拾うのが目的。

妥当性は要点照合 (scripts/judge_keyfact_coverage.py、Opus のシート × Sonnet の判定) を物差しにして
凍結窓で較正する (scripts/calibrate_fidelity.py)。連動しなければ採用しない。

限界: 日本語の固有名 (カタカナ・漢字の組織名) と、訳すと変わる語 (国名・業種) は数えない。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE)
_VERSION_RE = re.compile(r"\b\d+(?:\.\d+){2,}\b")
# 数値 + 桁の接尾辞 + 単位 (12k / 1.2 万 / 6.1TB / $12,000 / 80 件 など)
_NUM_RE = re.compile(
    r"(\$|US\$)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|K|M|B|万|億|千)?\s*"
    r"(TB|GB|MB|件|社|ドル|円|人|台|名|%|million|billion)?"
)
# 記事見出しの付記 (投稿者・日時) と日付は固有情報でない
_HEADER_META_RE = re.compile(r"\((?:@?[\w.\- ]+),\s*\d{4}-\d{2}-\d{2}T[^)]*\)")
_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:T[\d:.+Z-]+)?")
_STOP_TERMS = frozenset(
    {"cve", "cves", "tl", "dr", "tb", "gb", "mb", "kb", "poc", "url", "id", "ids", "faq"}
    | {
        m.lower()
        for m in [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
    }
)
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9&_\-]*[A-Za-z0-9]")
_SENT_START_RE = re.compile(r"(?:^|[.!?:;。]\s*|\n\s*|[-*•]\s+)$")
_SCALE = {"k": 1e3, "K": 1e3, "千": 1e3, "M": 1e6, "万": 1e4, "B": 1e9, "億": 1e8}
# 小さな数・年は文脈語 (「2 社」「2026 年」) が多く、固有情報として数えない
_MIN_NUMBER = 10.0
_YEAR_RANGE = (1990.0, 2100.0)
_NUM_REL_TOL = 0.01


@dataclass(frozen=True)
class Coverage:
    hit: int
    total: int
    missing: tuple[str, ...]

    @property
    def ratio(self) -> float | None:
        return self.hit / self.total if self.total else None


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def _numbers(text: str) -> set[float]:
    """数値を値に揃える。単位・通貨つきは小さくても数え、裸の数は 10 以上・年を除く。"""
    out: set[float] = set()
    for m in _NUM_RE.finditer(text):
        currency, raw, suffix, unit = (
            m.group(1),
            m.group(2).replace(",", ""),
            m.group(3),
            m.group(4),
        )
        try:
            value = float(raw) * _SCALE.get(suffix or "", 1.0)
        except ValueError:
            continue
        if unit in ("million", "billion"):
            value *= 1e6 if unit == "million" else 1e9
        marked = bool(currency or suffix or unit)
        is_year = not marked and raw.isdigit() and _YEAR_RANGE[0] <= value <= _YEAR_RANGE[1]
        if (marked or value >= _MIN_NUMBER) and not is_year and value > 0:
            out.add(value)
    return out


def _proper_latin(text: str) -> set[str]:
    """固有名らしいラテン文字の語。文頭以外で大文字始まり・全大文字 2 字以上・英字と数字の混在。

    一般語は除く: 同じ本文で小文字でも使われている語 (Security / security) は固有名とみなさない。
    """
    lowercase_words = {t for t in _LATIN_RE.findall(text) if t.islower()}
    out: set[str] = set()
    for m in _LATIN_RE.finditer(text):
        tok = m.group(0)
        low = tok.lower()
        if low in _STOP_TERMS or low in lowercase_words:
            continue
        mid_sentence = not _SENT_START_RE.search(text[: m.start()])
        capital = tok[0].isupper() and mid_sentence
        acronym = len(tok) >= 2 and tok.isupper()
        mixed = any(c.isdigit() for c in tok) or any(c.isupper() for c in tok[1:])
        if capital or acronym or mixed:
            out.add(low)
    return out


def salient_terms(text: str) -> tuple[set[str], set[float]]:
    """(文字列で照合する固有情報, 値で照合する数値)。"""
    text = _DATETIME_RE.sub(" ", _HEADER_META_RE.sub(" ", _nfkc(text)))
    terms = {c.upper() for c in _CVE_RE.findall(text)}
    terms |= set(_VERSION_RE.findall(text))
    stripped = _CVE_RE.sub(" ", _VERSION_RE.sub(" ", text))
    terms |= _proper_latin(stripped)
    return terms, _numbers(stripped)


def entity_coverage(sources: str, summary: str) -> Coverage:
    """元記事の固有情報のうち、要約に現れる割合。"""
    src_terms, src_nums = salient_terms(sources)
    summary_n = _nfkc(summary)
    hay = summary_n.lower()
    sum_nums = _numbers(_CVE_RE.sub(" ", _VERSION_RE.sub(" ", summary_n)))
    missing = [t for t in sorted(src_terms) if t.lower() not in hay]
    missing += [
        f"{v:g}"
        for v in sorted(src_nums)
        if not any(abs(v - s) <= _NUM_REL_TOL * v for s in sum_nums)
    ]
    total = len(src_terms) + len(src_nums)
    return Coverage(hit=total - len(missing), total=total, missing=tuple(missing))
