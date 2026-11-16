"""インドの数の単位 (lakh・crore) の換算の照合 (影の記録のみ、出力は変えない)。

背景: インドの英語報道は「₹500 crore」「2.5 lakh」「1 lakh crore」のように書く。
1 lakh = 10 万、1 crore = 1,000 万、1 lakh crore = 1 兆。ローカル LLM は桁を取り違えることがある。
原文から期待値を出し、訳文の日本語の数 (50億・1,000万・1兆・素の数字) に 1% 以内で一致する
ものがあるかを見る。ログには数値だけを出し、本文・訳文は出さない。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from src.logging_config import get_logger

INDIAN_UNITS_ENV = "TRANSLATE_INDIAN_UNITS_CHECK"

#: 期待値と訳文の数の相対許容差
RELATIVE_TOLERANCE = 0.01
#: ログに出す訳文の数の上限 (件数の暴走を防ぐ)
MAX_LOGGED_NUMBERS = 30

_log = get_logger(__name__)

_UNIT_SCALE = {"lakh": 1e5, "lakhs": 1e5, "crore": 1e7, "crores": 1e7}
_LAKH_CRORE_SCALE = 1e12

_EN_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_EN_AMOUNT_RE = re.compile(
    rf"(?<![\w.,])({_EN_NUM})\s*(lakh[\s-]+crores?|lakhs?|crores?)\b",
    re.IGNORECASE,
)

_JA_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_JA_CHAIN_RE = re.compile(rf"{_JA_NUM}(?:\s?(?:千万|兆|億|万|千)(?:{_JA_NUM})?)*")
_JA_TOKEN_RE = re.compile(rf"({_JA_NUM})\s?(千万|兆|億|万|千)?")
_JA_SCALE = {"兆": 1e12, "千万": 1e7, "億": 1e8, "万": 1e4}


@dataclass(frozen=True)
class IndianAmount:
    """原文の 1 つの金額・数量と、換算した期待値。"""

    expected: float
    matched: bool


@dataclass(frozen=True)
class IndianUnitsResult:
    """照合の結果。``amounts`` が空なら対象外 (原文にインドの単位が無い)。"""

    amounts: tuple[IndianAmount, ...]
    ja_numbers: tuple[float, ...]

    @property
    def applicable(self) -> bool:
        return bool(self.amounts)

    @property
    def mismatched(self) -> int:
        return sum(1 for a in self.amounts if not a.matched)


def indian_units_mode() -> str:
    """``TRANSLATE_INDIAN_UNITS_CHECK`` (0 / shadow、既定 0)。未知の値は 0。"""
    raw = os.environ.get(INDIAN_UNITS_ENV, "0").strip().lower()
    return "shadow" if raw == "shadow" else "0"


def extract_indian_amounts(source: str) -> list[float]:
    """原文から lakh / crore / lakh crore の金額・数量を数値 (換算後) で取り出す。"""
    values: list[float] = []
    for num_raw, unit_raw in _EN_AMOUNT_RE.findall(source):
        unit = unit_raw.lower()
        scale = (
            _LAKH_CRORE_SCALE if unit.startswith("lakh") and "crore" in unit else _UNIT_SCALE[unit]
        )
        values.append(float(num_raw.replace(",", "")) * scale)
    return values


def _chain_value(chain: str) -> float:
    """「1兆2,000億」「2.5万」「50億」「1,234」のような 1 つの連なりを数値にする。"""
    total = 0.0
    pending = 0.0
    for num_raw, unit in _JA_TOKEN_RE.findall(chain):
        num = float(num_raw.replace(",", ""))
        if unit == "千":
            pending += num * 1_000
        elif unit:
            total += (pending + num) * _JA_SCALE[unit]
            pending = 0.0
        else:
            pending += num
    return total + pending


def parse_japanese_numbers(text: str) -> list[float]:
    """訳文の数 (兆・億・万・千の位取り付きと素の数字) を数値にして順に返す。"""
    return [_chain_value(m.group(0)) for m in _JA_CHAIN_RE.finditer(text)]


def _is_close(expected: float, found: float) -> bool:
    return abs(found - expected) <= RELATIVE_TOLERANCE * expected


def check_indian_units(source: str, translated: str) -> IndianUnitsResult:
    """原文のインドの単位の期待値が、訳文の日本語の数にあるかを照合する。"""
    expected = extract_indian_amounts(source)
    if not expected:
        return IndianUnitsResult(amounts=(), ja_numbers=())
    found = parse_japanese_numbers(translated)
    amounts = tuple(
        IndianAmount(expected=e, matched=any(_is_close(e, f) for f in found)) for e in expected
    )
    return IndianUnitsResult(amounts=amounts, ja_numbers=tuple(found))


def shadow_check_indian_units(
    source: str, translated: str, *, article_id: str | None = None, model: str = ""
) -> None:
    """影の記録: 照合して ``translate_indian_units_check`` を出す。例外は握る・出力は変えない。"""
    try:
        result = check_indian_units(source, translated)
        if not result.applicable:
            return
        _log.info(
            "translate_indian_units_check",
            article_id=article_id,
            model=model,
            amounts=len(result.amounts),
            mismatched=result.mismatched,
            expected=[a.expected for a in result.amounts],
            unmatched=[a.expected for a in result.amounts if not a.matched],
            ja_numbers=list(result.ja_numbers[:MAX_LOGGED_NUMBERS]),
        )
    except Exception as exc:  # noqa: BLE001 - 影の記録は翻訳を止めない
        _log.warning("translate_indian_units_check_failed", error_type=type(exc).__name__)
