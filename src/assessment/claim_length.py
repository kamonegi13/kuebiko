"""台帳 claim の長さの関門と表示の縮小 (2026-11-20、11-19 監査 D1 P0-2b)。

改訂のたびに新しい被害組織を書き足して claim が伸び続け (最大 4,742 字)、状況総括の SIR 節を
6,000 字の途中で切らせていた。

- 書き込み (``LEDGER_CLAIM_LENGTH_GATE`` = 0 / shadow / 1): 上限 ``CLAIM_MAX_CHARS`` 超、
  または前回の先頭を保ったまま ``APPEND_GROWTH_MAX`` 字超伸びる改訂は、1 なら前回を保つ。
  shadow はログ ``ledger_claim_length_gate`` (字数と型だけ、本文なし) のみ。
  上限 400 字は 10-19 以降の改訂の 6.7% に当たる (D1 の点検で測定)。
- 表示 (``SLOT_CLAIM_DISPLAY_FIRST_SENTENCE`` = 0 / 1): ``DISPLAY_LONG_CHARS`` 字を超える claim は
  第 1 文 (… 付き) で描く。台帳の値は変えない。
"""

from __future__ import annotations

import os

from src.logging_config import get_logger
from src.synthesis.grounded.slot_render_extras import first_sentence

_log = get_logger(__name__)

CLAIM_MAX_CHARS = 400
APPEND_GROWTH_MAX = 80
DISPLAY_LONG_CHARS = 200
_PREFIX_CHARS = 40


def check_claim_length(prior: str, revised: str) -> str | None:
    """関門に当たる型 (``too_long`` / ``append_growth``)。当たらなければ None (純粋関数)。"""
    if len(revised) > CLAIM_MAX_CHARS:
        return "too_long"
    head = prior.strip()[:_PREFIX_CHARS]
    grew = len(revised) - len(prior)
    if head and revised.startswith(head) and grew > APPEND_GROWTH_MAX:
        return "append_growth"
    return None


def apply_claim_length_gate(prior: str, revised: str) -> str:
    """旗に応じて採る claim を返す (1 で当たれば前回、それ以外は改訂のまま)。"""
    mode = os.environ.get("LEDGER_CLAIM_LENGTH_GATE", "0").strip()
    if mode not in ("shadow", "1") or not revised or revised == prior:
        return revised
    kind = check_claim_length(prior, revised)
    if kind is None:
        return revised
    _log.info(
        "ledger_claim_length_gate",
        mode=mode,
        kind=kind,
        prior_chars=len(prior),
        revised_chars=len(revised),
    )
    return prior if mode == "1" else revised


def display_claim(claim: str) -> str:
    """長い claim を第 1 文で描く (旗 ``SLOT_CLAIM_DISPLAY_FIRST_SENTENCE``、既定 0)。"""
    if os.environ.get("SLOT_CLAIM_DISPLAY_FIRST_SENTENCE", "0").strip() != "1":
        return claim
    if len(claim) <= DISPLAY_LONG_CHARS:
        return claim
    return first_sentence(claim)


__all__ = [
    "APPEND_GROWTH_MAX",
    "CLAIM_MAX_CHARS",
    "apply_claim_length_gate",
    "check_claim_length",
    "display_claim",
]
