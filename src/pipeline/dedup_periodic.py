"""定期の発表 (日次まとめ・日付違いの勧告等) の別の号を hard の重複判定から外す判定 (AT-4)。

背景: hard 層 (cos >= 0.92) の誤りは 9 件中 7 件で、型は「同じ発行元の定期の発表
(シンクタンクの日次まとめ・CERT の日付違いの勧告) の別の号を同じ事象とみなして落とす」。
落とした記事は保存されない。見出しの日付・号・期間が**両方にあり、違う**ときだけ「別の号」とし、
日付等を除いた見出しの骨格が同じ (定型) ことも要る。片方でも鍵が無い・鍵が同じ (同じ号の再投稿)
なら外さない。

期間の判定は ``roundup_detector.report_period`` を再利用する (月次レポート)。
旗 ``DEDUP_PERIODIC_EXEMPT``: ``0`` (既定) / ``shadow`` (ログのみ) / ``1`` (hard で落とさない)。
"""

from __future__ import annotations

import difflib
import os
import re
from typing import Literal

from src.cti.roundup_detector import report_period

PERIODIC_EXEMPT_ENV = "DEDUP_PERIODIC_EXEMPT"
Mode = Literal["0", "shadow", "1"]

#: 日付を除いた見出しの骨格がこの割合以上同じなら「同じ定型の発表」とみなす。
_SKELETON_MIN_RATIO = 0.9

_MONTHS: dict[str, int] = {}
for _i, _names in enumerate(
    (
        ("january", "jan", "janvier"),
        ("february", "feb", "février", "fevrier"),
        ("march", "mar", "mars"),
        ("april", "apr", "avril"),
        ("may", "mai"),
        ("june", "jun", "juin"),
        ("july", "jul", "juillet"),
        ("august", "aug", "août", "aout"),
        ("september", "sep", "sept", "septembre"),
        ("october", "oct", "octobre"),
        ("november", "nov", "novembre"),
        ("december", "dec", "décembre", "decembre"),
    ),
    start=1,
):
    for _n in _names:
        _MONTHS[_n] = _i

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # October 9, 2026 / Oct 9 / October 9th
    re.compile(
        rf"\b(?P<m>{_MONTH_ALT})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<y>20\d{{2}}))?\b",
        re.IGNORECASE,
    ),
    # 16 octobre 2026 / 9 October
    re.compile(
        rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<m>{_MONTH_ALT})\.?(?:,?\s+(?P<y>20\d{{2}}))?\b",
        re.IGNORECASE,
    ),
    # 2026-10-09 / 2026/10/09
    re.compile(r"\b(?P<y>20\d{2})[-/.](?P<m>0?[1-9]|1[0-2])[-/.](?P<d>0?[1-9]|[12]\d|3[01])\b"),
    # 2026年10月9日 / 10月9日
    re.compile(r"(?:(?P<y>20\d{2})\s*年\s*)?(?P<m>1[0-2]|0?[1-9])\s*月\s*(?P<d>[0-3]?\d)\s*日"),
)
_ISSUE_PATTERN = re.compile(
    r"(?:\bissue\s*#?\s*(?P<a>\d+)|\bno\.?\s*(?P<b>\d+)|\bvol\.?\s*(?P<c>\d+)"
    r"|第\s*(?P<e>\d+)\s*[回号]|#\s*(?P<f>\d+)\b)",
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def periodic_mode() -> Mode:
    """``DEDUP_PERIODIC_EXEMPT`` を解決する (不明な値は 0 = 従来どおり)。"""
    raw = os.environ.get(PERIODIC_EXEMPT_ENV, "0").strip().lower()
    if raw == "shadow":
        return "shadow"
    if raw in ("1", "on", "true"):
        return "1"
    return "0"


def _month_number(token: str) -> int:
    return _MONTHS[token.lower()]


def _date_keys(text: str) -> tuple[frozenset[str], str]:
    """見出しの日付を ``MM-DD`` (年があれば ``YYYY-MM-DD``) の集合で返し、除去後の文字列も返す。"""
    keys: set[str] = set()
    rest = text
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(rest):
            month = m.group("m")
            mm = _month_number(month) if month.isalpha() else int(month)
            dd = int(m.group("d"))
            year = m.group("y")
            keys.add(f"{year}-{mm:02d}-{dd:02d}" if year else f"{mm:02d}-{dd:02d}")
        rest = pat.sub(" ", rest)
    return frozenset(keys), rest


def _issue_keys(text: str) -> tuple[frozenset[str], str]:
    keys = {f"issue:{next(g for g in m.groups() if g)}" for m in _ISSUE_PATTERN.finditer(text)}
    return frozenset(keys), _ISSUE_PATTERN.sub(" ", text)


def issue_key(title: str) -> tuple[frozenset[str], str]:
    """見出しから (号・日付・期間の鍵の集合, それらを除いた骨格) を返す。鍵が無ければ空集合。"""
    text = (title or "").strip()
    dates, rest = _date_keys(text)
    issues, rest = _issue_keys(rest)
    keys = set(dates) | set(issues)
    period = report_period(text)
    if period:
        keys.add(f"period:{period}")
    skeleton = _NON_WORD.sub(" ", rest).strip().casefold()
    return frozenset(keys), skeleton


def periodic_exempt_reason(title_a: str, title_b: str) -> str | None:
    """2 つの見出しが「定期の発表の別の号」なら理由を、違えば None を返す。

    両方に鍵があり、集合が異なり (同じ号の再投稿は外さない)、鍵を除いた骨格が同じ定型のとき。
    """
    keys_a, skel_a = issue_key(title_a)
    keys_b, skel_b = issue_key(title_b)
    if not keys_a or not keys_b or keys_a == keys_b:
        return None
    if not skel_a or not skel_b:
        return None
    if difflib.SequenceMatcher(None, skel_a, skel_b).ratio() < _SKELETON_MIN_RATIO:
        return None
    kinds = sorted({k.split(":", 1)[0] if ":" in k else "date" for k in keys_a ^ keys_b})
    return "different_issue:" + "+".join(kinds)
