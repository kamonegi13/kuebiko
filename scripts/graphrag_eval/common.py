"""E1・E2 の共通部品: Opus の呼び出し (adjudication_cache 経由) と回数の上限、prereg の関門、manifest。

回数の数え方は scripts/s26_imp/opus.py と同じ: キャッシュに当たる呼び出しは Opus を呼ばないので数えない。
キャッシュに無い呼び出しの試行 (再試行を含む) を 1 回ずつ数え、見積もりの 1.3 倍を超える前に止める。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from scripts.graphrag_eval.prompts import EFFORT, JUDGE

ROOT = Path(__file__).resolve().parents[2]
BUDGET_FACTOR = 1.3
RETRIES = 2
CONCURRENCY = 3


class BudgetExceededError(RuntimeError):
    """Opus の回数が見積もりの 1.3 倍を超える。"""


@dataclass
class OpusBudget:
    name: str
    estimate: int
    factor: float = BUDGET_FACTOR
    used: int = 0

    @property
    def limit(self) -> int:
        return int(self.estimate * self.factor)

    def charge(self, n: int = 1) -> None:
        if self.used + n > self.limit:
            raise BudgetExceededError(
                f"{self.name}: Opus {self.used + n} 回は見積もり {self.estimate} の "
                f"{self.factor} 倍 ({self.limit}) を超える。止めて報告する"
            )
        self.used += n


def adjudication_module() -> Any:
    mlx = str(ROOT / "data" / "mlx")
    if mlx not in sys.path:
        sys.path.insert(0, mlx)
    return importlib.import_module("adjudication_cache")


JudgeFn = Callable[[str, dict[str, Any]], Awaitable[BaseModel | None]]


def _count_bridge_calls(budget: OpusBudget) -> None:
    """ClaudeCodeClient._request を包み、bridge への実際の呼び出しを 1 回ずつ数える。

    キャッシュに当たる呼び出しは _request に届かないので数えない。構造化出力の内部の再試行も
    _request を通るので数える (見積もりの 1.3 倍の上限は実際の呼び出しの数で守る)。
    """
    from src.tools import claude_code_client as ccc

    if getattr(ccc.ClaudeCodeClient._request, "_graphrag_counted", False):  # noqa: SLF001
        ccc.ClaudeCodeClient._request._graphrag_budget = budget  # type: ignore[attr-defined]  # noqa: SLF001
        return
    original = ccc.ClaudeCodeClient._request  # noqa: SLF001

    async def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        counted._graphrag_budget.used += 1  # type: ignore[attr-defined]
        return await original(self, *args, **kwargs)

    counted._graphrag_counted = True  # type: ignore[attr-defined]
    counted._graphrag_budget = budget  # type: ignore[attr-defined]
    ccc.ClaudeCodeClient._request = counted  # type: ignore[method-assign]  # noqa: SLF001


def make_judge(
    schema: type[BaseModel], rubric: str, budget: OpusBudget, *, max_tokens: int
) -> JudgeFn:
    """1 件を判定する関数。失敗は RETRIES 回まで再試行し、なお失敗なら None。

    キャッシュに無い呼び出しの前に「使った数 + 1」が上限を超えるなら止める (BudgetExceededError)。
    数えるのは bridge への実際の呼び出し (_count_bridge_calls。始めた時点で数えるので飛行中も含む)。
    """
    ac = adjudication_module()
    _count_bridge_calls(budget)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def judge(question: str, inputs: dict[str, Any]) -> BaseModel | None:
        key = ac.make_key(question, inputs, schema, rubric=rubric, judge=JUDGE, effort=EFFORT)
        for _ in range(1 + RETRIES):
            async with sem:
                miss = ac._state.get_cached(key, ac.CACHE_PATH) is None  # noqa: SLF001
                if miss and budget.used + 1 > budget.limit:
                    raise BudgetExceededError(
                        f"{budget.name}: Opus {budget.used} 回で上限 {budget.limit} "
                        f"(見積もり {budget.estimate} の {budget.factor} 倍) に達する。止めて報告する"
                    )
                result: BaseModel | None = await ac.adjudicate(
                    question,
                    inputs,
                    schema,
                    rubric=rubric,
                    judge=JUDGE,
                    effort=EFFORT,
                    max_tokens=max_tokens,
                )
            if result is not None:
                return result
        return None

    return judge


def require_prereg(name: str, caller: Path, out_dir: Path) -> dict[str, str]:
    """prereg が HEAD の祖先で、道具が未変更のとき記録を返す (scripts/replay/_gate.py)。"""
    from scripts.replay._gate import require_prereg as gate

    rec = gate(name, ROOT, caller=caller, out_dir=out_dir)
    return {
        "prereg_path": rec.path,
        "prereg_git_sha": rec.git_sha,
        "prereg_blob_sha256": rec.blob_sha256,
        "head_sha": rec.head_sha,
        "prereg_commit_time": rec.commit_time,
        "run_started_at": rec.run_started_at,
    }


def assert_clean(*paths: str) -> None:
    out = subprocess.run(  # noqa: S603
        ["git", "-C", str(ROOT), "status", "--porcelain", "--", *paths],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if out:
        raise RuntimeError(f"未コミットの変更がある: {out}")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", "utf-8")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
