"""Opus の呼び出し (adjudication_cache 経由) と段ごとの回数の上限 (見積もりの 1.3 倍で止める、§9)。

キャッシュに当たる呼び出しは Opus を呼ばないので数えない。数えるのはキャッシュに無い呼び出しの試行
(再試行も 1 回と数える)。dry-run は数えるだけで Opus を呼ばない。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from scripts.s26_imp.common import (
    CONCURRENCY,
    DATA_ROOT,
    JUDGE,
    LEDGER_REL,
    MAX_TOKENS,
    RETRIES,
    ROOT,
    batch_inputs,
    question_for_pass,
    rubric_for_cache,
)

BUDGET_FACTOR = 1.3
#: 段ごとの Opus 回数の見積もり (prereg §9)
ESTIMATES = {"GOLD": 312, "PRECHECK": 52, "RELABEL": 327, "RP9-GOLD": 150}


class OpusStopError(RuntimeError):
    """Opus の呼び出しを止めて報告する (回数の上限・連続失敗の上限)。"""


class BudgetExceededError(OpusStopError):
    """段の Opus 回数が見積もりの 1.3 倍を超えた。"""


@dataclass
class OpusBudget:
    stage: str
    estimate: int
    factor: float = BUDGET_FACTOR
    used: int = 0
    log: list[str] = field(default_factory=list)

    @property
    def limit(self) -> int:
        return int(self.estimate * self.factor)

    def charge(self, n: int = 1) -> None:
        if self.used + n > self.limit:
            raise BudgetExceededError(
                f"{self.stage}: Opus {self.used + n} 回は見積もり {self.estimate} の "
                f"{self.factor} 倍 ({self.limit}) を超える。止めて報告する"
            )
        self.used += n


#: (問い, 入力, 札 → id) を受けて判定を返す関数 (None = 失敗)
JudgeFn = Callable[[str, dict[str, Any], str], Awaitable[BaseModel | None]]


def _adjudication_module() -> Any:
    mlx = str(DATA_ROOT / "data" / "mlx")
    if mlx not in sys.path:
        sys.path.insert(0, mlx)
    sys.path.insert(0, str(ROOT))
    return importlib.import_module("adjudication_cache")


def make_opus_judge(
    schema: type[BaseModel], instruction: str, *, effort: str, budget: OpusBudget
) -> JudgeFn:
    """adjudicate を包む。キャッシュに無ければ budget に数える。LimitReached はそのまま上げる。"""
    ac = _adjudication_module()
    rubric = rubric_for_cache(instruction)

    async def judge(question: str, inputs: dict[str, Any], _tag: str) -> BaseModel | None:
        key = ac.make_key(question, inputs, schema, rubric=rubric, judge=JUDGE, effort=effort)
        if ac._state.get_cached(key, ac.CACHE_PATH) is None:  # noqa: SLF001
            budget.charge(1)
        try:
            result: BaseModel | None = await ac.adjudicate(
                question,
                inputs,
                schema,
                rubric=rubric,
                judge=JUDGE,
                effort=effort,
                max_tokens=MAX_TOKENS,
            )
        except ac.LimitReached as exc:
            raise OpusStopError(f"連続失敗の上限: {exc}") from exc
        return result

    return judge


def plan_calls(
    groups: Sequence[Sequence[dict[str, Any]]],
    *,
    pass_no: int,
    schema: type[BaseModel],
    instruction: str,
    effort: str,
) -> tuple[int, int]:
    """dry-run 用: (組の数, キャッシュに無い組の数)。Opus は呼ばない。"""
    try:
        ac = _adjudication_module()
    except ImportError:
        return len(groups), len(groups)
    rubric = rubric_for_cache(instruction)
    question = question_for_pass(pass_no)
    miss = 0
    for g in groups:
        inputs, _ = batch_inputs(g)
        key = ac.make_key(question, inputs, schema, rubric=rubric, judge=JUDGE, effort=effort)
        if ac._state.get_cached(key, ac.CACHE_PATH) is None:  # noqa: SLF001
            miss += 1
    return len(groups), miss


async def judge_items(
    items: Sequence[Sequence[dict[str, Any]]],
    *,
    pass_no: int,
    judge: JudgeFn,
    verdict_attr: str = "items",
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """組ごとに判定する。戻り = (id → 判定の dict, 失敗した記事 id)。

    組が失敗 (None・id の欠け/取り違え) したら RETRIES 回まで再試行する。
    """
    sem = asyncio.Semaphore(CONCURRENCY)
    got: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    question = question_for_pass(pass_no)

    async def one(group: Sequence[dict[str, Any]]) -> None:
        inputs, labels = batch_inputs(group)
        for _ in range(1 + RETRIES):
            async with sem:
                v = await judge(question, inputs, f"pass{pass_no}")
            parsed = getattr(v, verdict_attr, None) if v is not None else None
            by = {x.id: x for x in parsed if x.id in labels} if parsed else {}
            if parsed and set(by) == set(labels) and len(parsed) == len(labels):
                for lab, x in by.items():
                    got[labels[lab]] = x.model_dump()
                return
        failed.extend(g["id"] for g in group)

    await asyncio.gather(*[one(g) for g in items])
    return got, failed


def append_ledger(item: str, rubric: str, budget: OpusBudget, **extra: Any) -> None:
    path = DATA_ROOT / LEDGER_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "time": datetime.now(UTC).isoformat(),
        "item": item,
        "judge": JUDGE,
        "rubric": rubric,
        "opus_calls_charged": budget.used,
        "budget_limit": budget.limit,
        **extra,
    }
    with Path(path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
