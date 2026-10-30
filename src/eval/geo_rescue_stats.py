"""地政学の救済の prompt 比較 (flat vs pir) の集計 (純関数、id のみを扱う)。"""

from __future__ import annotations

import math
from typing import Any


def mcnemar_exact(b: int, c: int) -> float:
    """exact 両側 McNemar (b, c = 不一致の 2 方向)。b + c = 0 は 1.0。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail: float = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def summarize(
    records: list[dict[str, Any]], strata: dict[str, str], gold: dict[str, bool]
) -> dict[str, Any]:
    """層別の件数と、正解のある id での flat / pir の McNemar をまとめる (純関数、id のみ)。"""
    per_stratum: dict[str, dict[str, int]] = {}
    for r in records:
        st = per_stratum.setdefault(
            strata.get(r["id"], "other"), {"n": 0, "flat_rescued": 0, "pir_rescued": 0}
        )
        st["n"] += 1
        st["flat_rescued"] += int(r["flat_rescued"])
        st["pir_rescued"] += int(r["pir_rescued"])
    graded = [r for r in records if r["id"] in gold]
    flat_ok = {r["id"]: r["flat_rescued"] == gold[r["id"]] for r in graded}
    pir_ok = {r["id"]: r["pir_rescued"] == gold[r["id"]] for r in graded}
    b = sum(1 for i in flat_ok if flat_ok[i] and not pir_ok[i])  # flat だけ正しい
    c = sum(1 for i in flat_ok if not flat_ok[i] and pir_ok[i])  # pir だけ正しい
    return {
        "per_stratum": per_stratum,
        "graded": len(graded),
        "flat_correct": sum(flat_ok.values()),
        "pir_correct": sum(pir_ok.values()),
        "flat_only_correct": b,
        "pir_only_correct": c,
        "mcnemar_p": mcnemar_exact(b, c),
    }
