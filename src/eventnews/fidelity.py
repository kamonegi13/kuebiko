"""要約の固有情報の網羅率 — LLM を使わない簡易な正しさの指標 (2026-09-26)。

事象ニュースが元記事の **訳しても変わらない固有情報** (CVE・版番号・数値・ラテン文字の固有名) を
どれだけ運んでいるかを文字列照合で数える。n17c で問題になった「国名・業種・CVE が潰れる」
種類の欠落を、追加の LLM 呼出なしで拾うのが目的。

妥当性は要点照合 (scripts/judge_keyfact_coverage.py、Opus のシート × Sonnet の判定) を物差しにして
凍結窓で較正する (scripts/calibrate_fidelity.py)。連動しなければ採用しない。

限界: 日本語の固有名 (カタカナ・漢字の組織名) と、訳すと変わる語 (国名・業種) は数えない。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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


# ---------- 生成物・プロンプトからの取り出し (本番 API と評価スクリプトで共有) ----------


def source_text(prompt: str) -> str:
    """事象ニュースのプロンプトから記事本文の部分だけを切り出す (無ければ全体)。"""
    start = prompt.find("## 対象記事")
    end = prompt.find("## 識別子カタログ", start + 1)
    if start < 0:
        return prompt
    return prompt[start : end if end > start else len(prompt)]


def draft_text(body: Mapping[str, Any], headline: str = "") -> str:
    """生成物 (EventNewsDraft の dict) を照合用の本文へ。読者に見える欄だけを並べる。"""
    lines = [f"見出し: {headline or body.get('headline', '')}", f"BLUF: {body.get('bluf', '')}"]
    for label, key in (("要点", "key_points"), ("不明点", "unknowns")):
        lines += [f"{label}: {x}" for x in body.get(key) or [] if isinstance(x, str)]
    for label, key in (("事実", "facts"), ("相違", "discrepancies"), ("但し書き", "caveats")):
        lines += [
            f"{label}: {x.get('text', '')}" for x in body.get(key) or [] if isinstance(x, dict)
        ]
    return "\n".join(lines)


def version_coverage(prompt_text: str, body_json: str, headline: str = "") -> Coverage | None:
    """1 版の網羅率。プロンプトが保存されていない版 (2026-08-26 以前) は None。"""
    if not prompt_text or not body_json:
        return None
    try:
        body = json.loads(body_json)
    except json.JSONDecodeError:
        return None
    cov = entity_coverage(source_text(prompt_text), draft_text(body, headline))
    return cov if cov.total else None


# ---------- 1 本ごとの表示: 抽出済み entity のうち要約に無いもの ----------

# 本文からの推測 (salient_terms) は 1 本の表示には雑音が多い (ドイツ語の名詞・日付・訳される地名)。
# 表示は抽出層が記事ごとに整えた entity を基準にする。国・TTP・PIR は訳語・分類なので対象外
GAP_ENTITY_TYPES = (
    "cve",
    "actor",
    "malware_family",
    "victim_org",
    "affected_product",
    "affected_vendor",
)
_MIN_HEAD_TOKEN = 3
_SEPARATORS_RE = re.compile(r"[\s\-_.・]")


def _compact(s: str) -> str:
    """空白・ハイフン等を除いて照合する (「checkpoint」と「Check Point」)。"""
    return _SEPARATORS_RE.sub("", s)


def entity_gaps(
    entities: Mapping[str, Iterable[str]], summary: str
) -> tuple[int, list[tuple[str, str]]]:
    """(照合した件数, 要約に無い (種類, 値))。大文字小文字・全半角は無視する。

    複数語の名前は先頭の語が出ていれば含むとみなす
    (「big-ip access policy manager」→ 要約「BIG-IP APM」)。
    """
    hay = _nfkc(summary).lower()
    hay_compact = _compact(hay)
    seen: set[str] = set()
    checked = 0
    missing: list[tuple[str, str]] = []
    for etype in GAP_ENTITY_TYPES:
        for value in entities.get(etype, ()):
            key = _nfkc(value).lower().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            checked += 1
            head = key.split()[0]
            head_hit = len(head) >= _MIN_HEAD_TOKEN and head in hay
            if key in hay or head_hit or _compact(key) in hay_compact:
                continue
            missing.append((etype, value))
    return checked, missing


# ---------- 週次の見張り (多数の版の平均で見る。1 本の点数としては使わない) ----------

# 前週より平均がこれ以上下がったら警告。較正 (2026-09-26) で n17m30 → n17c の劣化が約 5pt
WEEKLY_DROP_WARN = 0.05
_MIN_VERSIONS = 10


def weekly_line(this_week: Sequence[Coverage], last_week: Sequence[Coverage]) -> tuple[str, bool]:
    """週次監査の 1 行と、警告するか。平均は版ごとの網羅率の平均 (記事の多い版に引きずられない)。"""

    def _mean(xs: Sequence[Coverage]) -> float | None:
        vals = [c.ratio for c in xs if c.ratio is not None]
        return sum(vals) / len(vals) if len(vals) >= _MIN_VERSIONS else None

    cur, prev = _mean(this_week), _mean(last_week)
    if cur is None:
        return f"事象ニュース 固有情報の網羅率: 版が少ない ({len(this_week)} 版)", False
    line = f"事象ニュース 固有情報の網羅率: {cur:.0%} ({len(this_week)} 版)"
    if prev is None:
        return line, False
    warn = prev - cur >= WEEKLY_DROP_WARN
    return (
        f"{line} / 前週 {prev:.0%}{' ⚠️ 低下 — モデル・プロンプトの変更を確認' if warn else ''}",
        warn,
    )
