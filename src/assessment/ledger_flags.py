"""台帳の上流の修正 (docs/ledger_upstream_fixes_2026_11_17.md §1-3) の旗。

値は ``0`` / ``off`` (既定 = 従来と同一)・``shadow`` (計算して log / 記録だけ、出力は不変)・
``1`` / ``on`` (適用)。**未知の値は off**。旗は呼び出しのたびに環境変数から読む。
"""

from __future__ import annotations

import os
from typing import Literal

FlagMode = Literal["off", "shadow", "on"]

SEED_STRICT_ENV = "LEDGER_SEED_STRICT"
DUP_RESCUE_ENV = "LEDGER_DUP_RESCUE"
ASSIGN_STRONG_ANCHOR_ENV = "LEDGER_ASSIGN_STRONG_ANCHOR"

_ON = frozenset({"1", "on", "true"})


def _mode(env: str) -> FlagMode:
    raw = os.environ.get(env, "0").strip().lower()
    if raw in _ON:
        return "on"
    if raw == "shadow":
        return "shadow"
    return "off"


def seed_strict_mode() -> FlagMode:
    return _mode(SEED_STRICT_ENV)


def dup_rescue_mode() -> FlagMode:
    return _mode(DUP_RESCUE_ENV)


def assign_strong_anchor_mode() -> FlagMode:
    return _mode(ASSIGN_STRONG_ANCHOR_ENV)
