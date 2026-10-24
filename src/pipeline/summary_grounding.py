"""薄い Grok 投稿の要約が本文に無い事実を足していないかの決定論の関門 (2026-10-24)。

s24c の重い誤り (Opus 裁定) の主因は、本文が数百字の Grok 投稿に対し、要約が本文に無い
版番号・修正日・年・CVE を足すこと (ESXi の「7.0.2u4・2023 年 11 月 22 日」等)。薄い本文の関門
(``THIN_BODY_GATE``) は Grok を対象外にしており (短文が正常な形式)、止まらなかった。

方針は detect の ``claim_grounding`` と同じ型 (要約の具体的な語が本文に在るか) だが、固有名詞
(カタカナ・英字) は一般語の誤検出が多すぎる (Grok 564 件の 530 件が発火) ため使わず、
**数字の記号 (版番号・年・金額・CVE 番号・IP)** だけを照合する。
さらに「攻撃者の帰属の記載はない」と書きながら本文に ``Threat Actor:`` 欄がある矛盾を拾う。

換算 (万・億・兆・千の単位)・ハンドル名内の数字・投稿の年は正当な書き換えなので対象外にする。
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Literal

GateMode = Literal["on", "shadow", "off"]

#: 関門の動作 (on = 作り直して、なお違反なら LLM 要約を捨てる / shadow = 記録だけ / off = 無効)
GATE_ENV = "GROK_SUMMARY_GROUNDING"

#: この桁数 (数字だけ数えて) 未満の整数は照合しない (「4 件」「15 日」の偶然の一致を避ける)
_MIN_INTEGER_DIGITS = 3

#: 「45K」= 45,000 の略記
_K_UNIT = 1000

#: 数字の記号。直前が英字・@・数字・記号 (ハンドル名・ID・桁区切りの途中) なら別物として拾わない
_NUMBER = re.compile(r"(?<![A-Za-z@\d.,_-])(\d[\d,]*(?:\.\d+)*)([A-Za-z]{0,2}\d*)")
#: 万・億・兆・千・百の単位が続く数は換算 (17 BILLION → 170億) なので照合しない
_CJK_SCALE_AFTER = re.compile(r"\s*[万億兆千百]")
_TRAILING_ZERO_DECIMAL = re.compile(r"(\d)\.0+(?!\d)")

#: 「(攻撃者|アクター|帰属)… 記載/言及/示され… ない」の言い回し
_ABSENCE_CLAIM = re.compile(
    r"(攻撃者|アクター|帰属)[^。]{0,30}(記載|言及|示さ|明記|特定)[^。]{0,10}(ない|なし|いない|れていない)"
)
#: 本文の構造化欄 ("Threat Actor: CHAOS" / "Actor: #Redact")
_ACTOR_FIELD = re.compile(r"(?i)\b(?:threat\s+)?actor\s*:\s*\S")


def gate_mode() -> GateMode:
    """``GROK_SUMMARY_GROUNDING`` (既定 shadow)。未知の値は shadow として扱う。"""
    mode = os.environ.get(GATE_ENV, "shadow").strip().lower()
    if mode == "on":
        return "on"
    if mode == "off":
        return "off"
    return "shadow"


def _normalize_source(source: str) -> str:
    """NFKC・桁区切りの除去・「10.0」→「10」(整数と同値の小数を同一視)。"""
    folded = unicodedata.normalize("NFKC", source).replace(",", "")
    return _TRAILING_ZERO_DECIMAL.sub(r"\1", folded)


def _summary_numbers(summary: str) -> list[str]:
    """要約の照合対象の数字 (出現順・重複なし・桁区切り除去済み)。"""
    folded = unicodedata.normalize("NFKC", summary)
    out: list[str] = []
    for m in _NUMBER.finditer(folded):
        if _CJK_SCALE_AFTER.match(folded, m.end()):
            continue
        token = (m.group(1) + m.group(2)).rstrip(",.").replace(",", "")
        digits = re.sub(r"\D", "", token)
        if len(digits) >= _MIN_INTEGER_DIGITS or "." in token:
            out.append(token)
    return list(dict.fromkeys(out))


def _is_in_source(token: str, source: str) -> bool:
    canonical = _TRAILING_ZERO_DECIMAL.sub(r"\1", token)
    if canonical.isdigit():
        # 整数は URL の日付 (nd-20261019) の中でも裏付けとみなす
        return canonical in source or _is_in_source_as_k(canonical, source)
    return re.search(rf"(?<![\d.]){re.escape(canonical)}(?!\d|\.\d)", source) is not None


def _is_in_source_as_k(digits: str, source: str) -> bool:
    """45000 を本文の「45K」で裏付ける (千単位の略記)。"""
    value = int(digits)
    if value < _K_UNIT or value % _K_UNIT:
        return False
    return re.search(rf"(?<![\d.]){value // _K_UNIT}\s?k\b", source, re.IGNORECASE) is not None


def ungrounded_numbers(
    summary: str, *, source: str, exempt_years: tuple[int, ...] = ()
) -> tuple[str, ...]:
    """要約にあって ``source`` (見出し+本文) に無い数字。``exempt_years`` は照合しない年。"""
    normalized = _normalize_source(source)
    exempt = {str(y) for y in exempt_years}
    return tuple(
        token
        for token in _summary_numbers(summary)
        if token not in exempt and not _is_in_source(token, normalized)
    )


_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_STANDALONE_INT = re.compile(r"(?<![\d.])0*(\d{1,2})(?![\d])")
_EMBEDDED_YMD = re.compile(r"(?<!\d)(?:19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])")
_MONTH_NAMES = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
_MONTH_WORD = re.compile(r"(?i)\b(" + "|".join(_MONTH_NAMES) + r")[a-z]*\b")


def _source_months_and_days(source: str) -> tuple[set[int], set[int]]:
    """本文に現れる月 (数字・英語名) と日 (1-2 桁の整数) の集合。"""
    ints = {int(m.group(1)) for m in _STANDALONE_INT.finditer(source)}
    # URL・ID に埋まった日付 (nd-20261019de61442、AKR20261020157851504)
    for m in _EMBEDDED_YMD.finditer(source):
        ints.update((int(m.group(1)), int(m.group(2))))
    months = {n for n in ints if 1 <= n <= 12}
    months.update(_MONTH_NAMES.index(m.group(1).lower()) + 1 for m in _MONTH_WORD.finditer(source))
    return months, ints


def ungrounded_dates(summary: str, *, source: str) -> tuple[str, ...]:
    """要約の「M 月 D 日」のうち、月か日が本文のどこにも無いもの (年が一致しても拾う)。"""
    months, days = _source_months_and_days(_normalize_source(source))
    out: list[str] = []
    for m in _MONTH_DAY.finditer(unicodedata.normalize("NFKC", summary)):
        month, day = int(m.group(1)), int(m.group(2))
        if month not in months or day not in days:
            out.append(f"{month}/{day}")
    return tuple(dict.fromkeys(out))


def absent_actor_contradiction(summary: str, body: str) -> bool:
    """「攻撃者の記載はない」と書きながら、本文に攻撃者の欄がある矛盾。"""
    return bool(_ABSENCE_CLAIM.search(summary)) and bool(_ACTOR_FIELD.search(body))


def ungrounded_reasons(
    summary: str, *, body: str, title: str = "", exempt_years: tuple[int, ...] = ()
) -> list[str]:
    """要約が本文で裏付けられない理由の一覧 (空なら問題なし)。純粋関数。"""
    reasons = [
        f"number:{token}"
        for token in ungrounded_numbers(
            summary, source=f"{title}\n{body}", exempt_years=exempt_years
        )
    ]
    reasons.extend(f"date:{d}" for d in ungrounded_dates(summary, source=f"{title}\n{body}"))
    if absent_actor_contradiction(summary, body):
        reasons.append("absent_actor_contradicts_body")
    return reasons
