"""n20 の本番の構造変更 (契約 T5) の旗。すべて既定 off = いまの出力と同一。

契約: data/eval/n20_contract_2026_11_02.md §3 T5・§4 U-A/U-B。設計: n20_s26_design_2026_10_29.md §2.2。

| 旗 | 対象 | off (既定) | shadow | on |
|---|---|---|---|---|
| ``FACT_ANCHOR`` | 事象ニュース | eventnews-v6 | v6 で配信 + v7 (anchor つき) を別に生成して関門の数だけ log | v7 + anchor の関門 |
| ``SLOT_RENDER`` | 状況総括の render | render.j2 | 旧で配信 + v9 (節の部品) を別に生成して log | v9 + 確度語・反証・対立仮説をコードが描く |
| ``SPOTLIGHT_STAGED`` | Spotlight | 1 段の生成 | 1 段で配信 + 多段を別に生成して所要と数を log | 多段 (a・b を LLM、(c) は対応表、(d) なし) |
| ``DEEP_DIVE_SLOT_RENDER`` | 深掘りの本文 | 節の散文 | 散文で配信 + 要点の形を別に生成して log | 要点の構造 → 文はコードが組む |

値は ``0`` / ``off`` (既定)・``shadow``・``1`` / ``on``。**未知の値は off** (本番の挙動に倒す)。
shadow は LLM の呼び出しが増える (配信は変わらない)。旗を on にする切替は SW-N20 の側で行う。
"""

from __future__ import annotations

import os
from typing import Literal

FlagMode = Literal["off", "shadow", "on"]

FACT_ANCHOR_ENV = "FACT_ANCHOR"
SLOT_RENDER_ENV = "SLOT_RENDER"
SPOTLIGHT_STAGED_ENV = "SPOTLIGHT_STAGED"
DEEP_DIVE_SLOT_RENDER_ENV = "DEEP_DIVE_SLOT_RENDER"
#: 多段 Spotlight の全体の上限 (秒)。未設定なら本番の step の上限 (STEP_REGISTRY の
#: PIR_SPOTLIGHT = 600 秒) と同じ。⚠ 未決: 設計 D は 3,000 秒、本番は 600 秒 (契約 §5-10)。
#: shadow の実測 (log ``spotlight_staged_shadow`` の elapsed_s) を見て決める。
SPOTLIGHT_STAGED_TIMEOUT_ENV = "SPOTLIGHT_STAGED_TIMEOUT_SECONDS"

_ON = frozenset({"1", "on", "true"})
_SHADOW = frozenset({"shadow"})


def _mode(env: str) -> FlagMode:
    raw = os.environ.get(env, "0").strip().lower()
    if raw in _ON:
        return "on"
    if raw in _SHADOW:
        return "shadow"
    return "off"


def fact_anchor_mode() -> FlagMode:
    return _mode(FACT_ANCHOR_ENV)


def slot_render_mode() -> FlagMode:
    return _mode(SLOT_RENDER_ENV)


def spotlight_staged_mode() -> FlagMode:
    return _mode(SPOTLIGHT_STAGED_ENV)


def deep_dive_slot_render_mode() -> FlagMode:
    return _mode(DEEP_DIVE_SLOT_RENDER_ENV)


def spotlight_staged_timeout_seconds() -> float:
    """多段 Spotlight の全体の上限。未設定・不正・0 以下は本番の step の上限。"""
    from src.tools.model_tiers import STEP_REGISTRY, Step

    default = STEP_REGISTRY[Step.PIR_SPOTLIGHT].timeout_seconds
    raw = os.environ.get(SPOTLIGHT_STAGED_TIMEOUT_ENV, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default
