"""ATT&CK テクニック ID の軽量判定 (ttp catalog 未照合 gap の決定論的な最小解)。

``ttp`` は Enterprise (T1xxx) と ICS (T0xxx) が同じ ``T\\d{4}`` 形式で保存される
(ioc_extractor の regex は両方を値で拾う)。両者は**名前空間で決定論的に区別できる** — tag survey が
「T-id から読み時導出可能」として tactic の entity 化を却下したのと同思想。ICS は
system_domain の OT 信号に使う (docs/jp_critical_infra_board_design.md §6-7)。

完全な catalog 照合 (T-id → name/tactic) は ics-attack.json fetch が要り本 board のスコープ外。
ここでは board に必要な ICS/Enterprise の名前空間分類のみを提供する。
"""

from __future__ import annotations

import re

_TID_RE = re.compile(r"^T(\d{4})(?:\.\d{3})?$")


def is_ics_technique(tid: str | None) -> bool:
    """ICS ATT&CK テクニック (T0xxx) か。Enterprise (T1xxx) と名前空間で区別 (catalog 不要)。"""
    if not tid:
        return False
    m = _TID_RE.match(tid.strip().upper())
    if m is None:
        return False
    return m.group(1).startswith("0")


def technique_matrix(tid: str | None) -> str:
    """テクニック ID → matrix ("ics" | "enterprise" | "unknown")。"""
    if not tid:
        return "unknown"
    m = _TID_RE.match(tid.strip().upper())
    if m is None:
        return "unknown"
    return "ics" if m.group(1).startswith("0") else "enterprise"
