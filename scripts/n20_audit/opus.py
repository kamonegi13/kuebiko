"""Opus の呼び出し (data/mlx/adjudication_cache.py 経由) と段ごとの回数の上限 (見積もりの 1.3 倍)。

キャッシュに当たる呼び出しは Opus を呼ばないので数えない。数えるのはキャッシュに無い呼び出しの試行。
連続失敗の上限 (LimitReached) で止める。拒否 (安全機能) はその 1 件を失敗として続ける
(opus_safe.py と同じ考え)。
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from scripts.n20_audit.common import DATA_ROOT, EFFORT, JUDGE, ROOT

BUDGET_FACTOR = 1.3
#: 段ごとの Opus の見積もり (契約 §3: T3 = 基線 90 + 自己一致 120 + 確認 60、T4 = 30〜60 の中間 45)
ESTIMATES = {"T3-BASE": 90, "T3-SELF": 120, "T3-CONFIRM": 60, "T4": 45}
REFUSAL_MARKERS = ("refusal", "拒否", "分類器", "classifier")


class OpusStopError(RuntimeError):
    """Opus の呼び出しを止めて報告する (回数の上限・連続失敗の上限)。"""


@dataclass
class OpusBudget:
    stage: str
    estimate: int
    factor: float = BUDGET_FACTOR
    used: int = 0
    cache_hits: int = 0
    refused: int = 0
    failed: int = 0

    @property
    def limit(self) -> int:
        return int(self.estimate * self.factor)

    def charge(self) -> None:
        if self.used + 1 > self.limit:
            raise OpusStopError(
                f"{self.stage}: Opus {self.used + 1} 回は見積もり {self.estimate} の "
                f"{self.factor} 倍 ({self.limit}) を超える。止めて報告する"
            )
        self.used += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "estimate": self.estimate,
            "limit": self.limit,
            "opus_calls_charged": self.used,
            "cache_hits": self.cache_hits,
            "refused": self.refused,
            "failed": self.failed,
        }


def budget_for(stage: str) -> OpusBudget:
    return OpusBudget(stage=stage, estimate=ESTIMATES[stage])


def _ac() -> Any:
    mlx = str(DATA_ROOT / "data" / "mlx")
    if mlx not in sys.path:
        sys.path.insert(0, mlx)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("adjudication_cache")


async def judge(
    question: str,
    inputs: dict[str, Any],
    schema: type[BaseModel],
    *,
    rubric: str,
    budget: OpusBudget,
    max_tokens: int,
) -> BaseModel | None:
    """1 件を裁定する。None = 失敗または拒否 (数えて続ける)。上限は OpusStopError。"""
    ac = _ac()
    key = ac.make_key(question, inputs, schema, rubric=rubric, judge=JUDGE, effort=EFFORT)
    if ac._state.get_cached(key, ac.CACHE_PATH) is None:  # noqa: SLF001
        budget.charge()
    else:
        budget.cache_hits += 1
    try:
        result: BaseModel | None = await ac.adjudicate(
            question,
            inputs,
            schema,
            rubric=rubric,
            judge=JUDGE,
            effort=EFFORT,
            max_tokens=max_tokens,
        )
    except ac.LimitReached as exc:
        if any(m in str(exc) for m in REFUSAL_MARKERS):
            ac._state.streak = 0  # noqa: SLF001 — 拒否は上限ではない
            budget.refused += 1
            return None
        raise OpusStopError(f"連続失敗の上限 (利用上限の可能性): {str(exc)[:300]}") from exc
    if result is None:
        budget.failed += 1
    return result
