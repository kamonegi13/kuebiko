"""毎時ジョブのチェーン実行 (2026-09-15、ジョブ実行要領の見直し B)。

背景: 毎時の interval ジョブ 11 本がオフセットで並び、伸びたジョブが次のジョブと重なり、
Ollama のモデル切替が 1 日 34 回発生していた (docs/ops/job_schedule_review_2026_09_15.md)。
モデル順に並べた直列チェーンにまとめ、重なりをなくし切替を 1 時間に数回へ整理する。

設計:
- 段は宣言順に直列。**1 段の失敗・timeout は隔離して次段へ** (チェーン全体を止めない)。
- 段ごとに job_run_log へ (job_id, status, detail) を記録する — 段の所要が見える。
- 段の timeout は段自身の max_runtime (registry) を使う。
- 段の実体 (callable) はコード所有。pipeline 段は subprocess run を起動し完了を待つ (app 側で合成)。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

_log = structlog.get_logger(__name__)

RecordFn = Callable[[str, str, str], None]


@dataclass(frozen=True)
class ChainStep:
    job_id: str
    run: Callable[[], Awaitable[object]]
    timeout_seconds: float


@dataclass(frozen=True)
class ChainResult:
    chain_id: str
    failed: tuple[str, ...]
    elapsed_seconds: float

    @property
    def ok(self) -> bool:
        return not self.failed


async def run_chain(chain_id: str, steps: list[ChainStep], *, record: RecordFn) -> ChainResult:
    """段を直列に実行し、段ごとの成否と所要を記録する。"""
    failed: list[str] = []
    started = time.monotonic()
    for step in steps:
        t0 = time.monotonic()
        status = "succeeded"
        detail = ""
        try:
            await asyncio.wait_for(step.run(), timeout=step.timeout_seconds)
        except TimeoutError:
            status = "failed"
            detail = f"timeout ({step.timeout_seconds:.0f}s)"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 段の失敗は隔離して次段へ (記録は残す)
            status = "failed"
            detail = f"{type(e).__name__}: {e}"
        elapsed = time.monotonic() - t0
        if status == "failed":
            failed.append(step.job_id)
            _log.warning("job_chain_step_failed", chain=chain_id, step=step.job_id, error=detail)
        else:
            _log.info(
                "job_chain_step_done", chain=chain_id, step=step.job_id, elapsed=round(elapsed)
            )
        record(step.job_id, status, f"chain={chain_id} elapsed={elapsed:.0f}s {detail}".strip())
    total = time.monotonic() - started
    _log.info("job_chain_done", chain=chain_id, failed=failed, elapsed=round(total))
    return ChainResult(chain_id=chain_id, failed=tuple(failed), elapsed_seconds=total)
