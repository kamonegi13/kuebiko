"""予測較正の外部会計 (F′: 説明責任の反転、2026-08-22)。

synthesis は予測 (indicators) を出し、その発火 (hit) も確度 (confidence) も**自分で
申告する**。正解が無い場に自己申告だけが並ぶため、外から検算する術が無かった。
本モジュールは決定論 (SQL + 算術のみ、LLM 呼出なし) で会計を付ける。

**判定するのは「当たったか」ではない** — 正解が無いので判定できない。判定するのは
**自己申告どうしの整合**: 「高確度と申告した状況の予測は、低確度より多く発火したか」。
順序が崩れる / 差が Wilson CI 内に収まるなら、確度表示は情報を持たない (装飾)。

⚠ **番兵専用 — 目標関数にしない**。「較正を良く見せる」最適化は、確度を出し渋る等の
Goodhart を招く (較正格子の壁 3 と同型)。報告のみに使う。

実測 (導入時): low 60.6% (n=284) > high 50.0% (n=250) > moderate 45.4% (n=207)。
**順序が逆転**しており、確度は自分の発火率を予測できていなかった。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

# 確度の強い順 (この順に発火率が下がるのが「情報を持つ」状態)
CONFIDENCE_ORDER: tuple[str, ...] = ("high", "moderate", "low")
# これ未満の標本では順序を論じない (consistency_sentinel の教訓: n=4 で 75% は無意味)
_MIN_CELL_SAMPLE = 30
_Z = 1.96  # 95% 両側


@dataclass(frozen=True)
class CalibrationCell:
    """確度 1 段あたりの実績 (点推定 + Wilson 95% CI)。"""

    confidence: str
    scored: int
    hit: int
    lo: float
    hi: float

    @property
    def rate(self) -> float:
        return self.hit / self.scored if self.scored else 0.0


def wilson_interval(hit: int, n: int, *, z: float = _Z) -> tuple[float, float]:
    """二項比率の Wilson 95% CI。標本ゼロは (0.0, 1.0) = 何も言えない。"""
    if n <= 0:
        return (0.0, 1.0)
    p = hit / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def is_ordering_informative(cells: list[CalibrationCell]) -> bool:
    """確度が発火率を予測できているか (単調降順 かつ 端の CI が重ならない)。

    3 段そろわない / いずれかの標本が小さい / 順序が崩れる / high と low の CI が
    重なる、のいずれでも False (= 確度は情報を持たない)。
    """
    by_conf = {c.confidence: c for c in cells}
    if any(k not in by_conf for k in CONFIDENCE_ORDER):
        return False
    ordered = [by_conf[k] for k in CONFIDENCE_ORDER]
    if any(c.scored < _MIN_CELL_SAMPLE for c in ordered):
        return False
    rates = [c.rate for c in ordered]
    if not (rates[0] >= rates[1] >= rates[2]):
        return False
    # 端どうしの CI が重なるなら「差がある」と主張できない
    return ordered[0].lo > ordered[-1].hi


def collect_calibration(store: Any) -> list[CalibrationCell]:
    """採点済み予測を「開設時の自己申告確度」で層別する (決定論・LLM 非経由)。"""
    rows = store.forecast_calibration_rows()
    cells: list[CalibrationCell] = []
    for r in rows:
        n = int(r["scored"] or 0)
        hit = int(r["hit"] or 0)
        lo, hi = wilson_interval(hit, n)
        cells.append(
            CalibrationCell(confidence=str(r["confidence"]), scored=n, hit=hit, lo=lo, hi=hi)
        )
    return cells


def calibration_lines(cells: list[CalibrationCell]) -> list[str]:
    """週次監査へ出す行 (自己採点の隣に置く外部会計)。"""
    known = [c for c in cells if c.confidence in CONFIDENCE_ORDER]
    if not known:
        return []
    total = sum(c.scored for c in known)
    if total < _MIN_CELL_SAMPLE:
        return [f"予測較正: 採点済 {total} 件 — 標本不足のため判定しない"]
    order = {k: i for i, k in enumerate(CONFIDENCE_ORDER)}
    known.sort(key=lambda c: order.get(c.confidence, 9))
    detail = " / ".join(
        f"{c.confidence} {c.rate:.0%} (CI {c.lo:.0%}-{c.hi:.0%}, n={c.scored})" for c in known
    )
    informative = is_ordering_informative(known)
    verdict = (
        "確度は発火率を予測できている"
        if informative
        else "⚠️ 確度が発火率を予測できていない (順序が崩れる/差が CI 内) — 確度表示は情報を持たない"
    )
    _log.info("forecast_calibration", informative=informative, scored=total)
    return [f"予測較正 (自己申告の確度 × 自己申告の発火): {detail}", f"  → {verdict}"]
