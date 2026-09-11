"""spotlight 尾部欄 (caveats / unknowns) の最小件数関門 (2026-09-11)。

背景: SFT 蒸留モデルは JSON 末尾のリスト欄を系統的に under-produce する
(教師 Opus は 179 対でゼロ件なし・中央値 5、生徒は 4/6 で空リスト —
docs/research/llm_training/sft_transfer_failure_diagnosis.md §2.1)。
終端判断は少数トークンに乗るため、指示 (rubric) では埋まらない。
事象ニュースの識別子関門と同じ思想で、**指示でなく関門**として同一プロンプトを
再サンプルし、尾部が最も充足した候補を採用する。

方針:
- 最小件数を下回る候補は再サンプル (上限 MAX_RESAMPLES 回)。上限後も下回れば
  **保存は止めず**、最良候補を採用して記録する (週次成果物の可用性を優先、
  未充足の計数は学習側の改善効果の測定材料)。
- 旗 ``SPOTLIGHT_TAIL_GATE=0`` で無効 (rollback)。
"""

from __future__ import annotations

import os
from typing import Protocol

GATE_FLAG_ENV = "SPOTLIGHT_TAIL_GATE"
# 教師分布 (ゼロ件 0/179・中央値 5) に対する保守的な下限。「空 or 1 件」を拾う。
MIN_CAVEATS = 2
MIN_UNKNOWNS = 2
MAX_RESAMPLES = 2


class _HasTail(Protocol):
    caveats: list[str]
    unknowns: list[str]


def gate_enabled() -> bool:
    """関門の有効判定 (既定 ON)。``SPOTLIGHT_TAIL_GATE=0`` で無効。"""
    return os.environ.get(GATE_FLAG_ENV, "1").strip() not in ("0", "false", "False")


def tail_deficit(output: _HasTail) -> tuple[str, ...]:
    """最小件数を下回る欄名を返す (空なら充足)。"""
    deficit: list[str] = []
    if len(output.caveats) < MIN_CAVEATS:
        deficit.append("caveats")
    if len(output.unknowns) < MIN_UNKNOWNS:
        deficit.append("unknowns")
    return tuple(deficit)


def tail_score(output: _HasTail) -> int:
    """候補比較用の充足度 (件数の単純和。最良候補の選択にのみ使う)。"""
    return len(output.caveats) + len(output.unknowns)
