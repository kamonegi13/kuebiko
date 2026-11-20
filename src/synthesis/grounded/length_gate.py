"""状況総括の長さの関門 (2026-11-20、docs/synthesis_length_gate.md §2.2-2.4)。

旗 ``SYNTHESIS_LENGTH_GATE=0|shadow|1`` (コードの既定 0、compose の既定 1)。

- 節が上限を超えたら、**salience の低い末尾の判定から 1 件ずつ簡潔な形に替える**
  (並びと件数は変えない)。暫定 (``trim_to_boundary``) は末尾の行から落とすので、SIR 保証で
  引き上げた判定と「そのほかの動き」の段落から先に消えていた。全部を簡潔にしても超えるときだけ
  行を縮め、段落は付け直す。
- 部品の途中切れは、クライアントの信号 (``llm_truncation``) で判定する。信号を出さない
  クライアント (外部) のときだけ従来の推測 (``watch_clause`` が空) に倒す。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from src.tools.llm_truncation import TruncationProbe
from src.tools.text_budget import trim_to_boundary

FLAG = "SYNTHESIS_LENGTH_GATE"
Mode = Literal["off", "shadow", "on"]


def length_gate_mode() -> Mode:
    raw = os.environ.get(FLAG, "0").strip().lower()
    if raw == "shadow":
        return "shadow"
    if raw in {"1", "on", "true"}:
        return "on"
    return "off"


@dataclass(frozen=True)
class FitResult:
    text: str
    #: 簡潔な形に替えた判定の数
    compacted: int
    #: 全部を簡潔にしても超え、行の境界で縮めたときに省いた行の数
    trimmed: int


def _join(lines: Sequence[str], tail: str) -> str:
    return "\n".join([*lines, tail] if tail else lines)


def fit_judgment_lines(
    full: Sequence[str], compact: Sequence[str], *, cap: int, tail: str = ""
) -> FitResult:
    """判定の行 (全文 / 簡潔の 2 形、salience 順) と末尾の段落を ``cap`` 字に収める (純粋関数)。"""
    lines = list(full)
    compacted = 0
    for i in reversed(range(len(lines))):
        if len(_join(lines, tail)) <= cap:
            break
        if compact[i] != lines[i]:
            lines[i] = compact[i]
            compacted += 1
    text = _join(lines, tail)
    if len(text) <= cap:
        return FitResult(text, compacted, 0)
    room = cap - (len(tail) + 1 if tail else 0)
    body, trimmed = trim_to_boundary("\n".join(lines), max(room, 0))
    return FitResult(_join([body], tail), compacted, trimmed)


def should_drop_last_item(
    *, has_items: bool, watch_empty: bool, probe: TruncationProbe, mode: Mode
) -> bool:
    """部品の最後の項目を途中切れとして捨てるか。

    出力は headline_clause → items → watch_clause の順に書かれるので、途中切れなら最後の
    項目だけが壊れている (それより前は閉じた文)。
    """
    if not has_items:
        return False
    if mode == "on" and probe.observed:
        return probe.truncated
    return watch_empty


__all__ = [
    "FLAG",
    "FitResult",
    "Mode",
    "fit_judgment_lines",
    "length_gate_mode",
    "should_drop_last_item",
]
