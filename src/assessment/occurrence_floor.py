"""``LEDGER_OCCURRENCE_FLOOR`` (``0|shadow|1``、設計 §5): 発生の確度を帰属の確度と分けて持つ。

確度は ACH の見立て (誰が・何の目的で) の確度で、事案が起きたことの確度ではない。見立てが
「未確認・虚偽」のとき被害組織の公表は neutral になり強帰属の引き上げも効かず、公表を根拠に
持つ新規の 77% が低確度から始まっていた。帰属の確度 (従来の値) は変えず、事案の型が単発事象で
被害組織の公表・政府の確認があれば、発生の確度 (moderate 下限、独立 2 記事で high) を
``confidence_basis`` の印として別に記録する。

- コードの既定 0 = 出力は従来と同一 / shadow = ログ ``ledger_occurrence_floor_shadow`` のみ /
  1 = 印を ``confidence_basis`` に足し (``KeyJudgment.occurrence_confirmed`` が立つ)、
  ``salience.epistemic_weight`` が max(帰属の確度, 発生の確度) で測る。
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Literal

from src.logging_config import get_logger
from src.synthesis.grounded.estimate import KeyJudgment, occurrence_floor

_log = get_logger(__name__)
FLAG_ENV = "LEDGER_OCCURRENCE_FLOOR"
Mode = Literal["0", "shadow", "1"]


def occurrence_floor_mode() -> Mode:
    v = os.environ.get(FLAG_ENV, "0").strip().lower()
    return v if v in ("shadow", "1") else "0"  # type: ignore[return-value]


def apply_occurrence_floor(j: KeyJudgment) -> KeyJudgment:
    """旗に従い発生の印を足した KeyJudgment (旗 0・対象外は同じ object。確度は変えない)。"""
    mode = occurrence_floor_mode()
    if mode == "0":
        return j
    floor = occurrence_floor(j.claim_type, j.evidence)
    if floor is None:
        return j
    occurrence, mark = floor
    _log.info(
        "ledger_occurrence_floor_shadow",
        situation=j.id,
        confidence=j.confidence,
        occurrence_confidence=occurrence,
        basis=mark,
        mode=mode,
    )
    if mode != "1":
        return j
    basis = f"{j.confidence_basis} / {mark}" if j.confidence_basis else mark
    return replace(j, confidence_basis=basis)
