"""毎時ジョブのチェーン実行 (2026-09-15、ジョブ実行要領の見直し B)。

背景: 毎時の interval ジョブ 11 本がオフセットで並び、伸びたジョブが次のジョブと重なり、
Ollama のモデル切替が 1 日 34 回発生していた (docs/ops/job_schedule_review_2026_09_15.md)。
モデル順に並べた直列チェーンにまとめ、重なりをなくし切替を 1 時間に数回へ整理する。

設計:
- 段は宣言順に直列。**1 段の失敗・timeout は隔離して次段へ** (チェーン全体を止めない)。
- 段ごとに job_run_log へ (job_id, status, detail) を記録する — 段の所要が見える。
- 段の timeout は段自身の max_runtime (registry) を使う。
- 段の実体 (callable) はコード所有。pipeline 段は subprocess run を起動し完了を待つ (app 側で合成)。
- 段は実行枠 (job_running) を取ってから走る。手動で同じ段が走っていれば **その段は飛ばす**
  (2026-09-26: 手動起動とチェーンが同じ段を並行実行していた)。段の開始は ``mark_start`` で
  DB に running として書く — 画面の「実行中」表示と、別プロセス (readonly) からの参照用。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from src.scheduler.job_running import running_slot

_log = structlog.get_logger(__name__)

RecordFn = Callable[[str, str, str], None]
MarkStartFn = Callable[[str], None]


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
    skipped: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed


async def run_chain(
    chain_id: str,
    steps: list[ChainStep],
    *,
    record: RecordFn,
    mark_start: MarkStartFn | None = None,
) -> ChainResult:
    """段を直列に実行し、段ごとの成否と所要を記録する。"""
    failed: list[str] = []
    skipped: list[str] = []
    started = time.monotonic()
    for step in steps:
        async with running_slot(step.job_id) as acquired:
            if not acquired:
                # 手動実行などで同じ段が走っている。記録は走っている側が書くので上書きしない
                skipped.append(step.job_id)
                _log.warning("job_chain_step_skipped_running", chain=chain_id, step=step.job_id)
                continue
            if mark_start is not None:
                try:
                    mark_start(step.job_id)
                except Exception as e:  # noqa: BLE001 — 表示用の記録で段を止めない
                    _log.warning("job_chain_mark_start_failed", step=step.job_id, error=str(e))
            try:
                status, detail, elapsed = await _run_step(step)
            except BaseException:
                # 中断 (シャットダウン等) でも記録を書く。書かないと mark_start の running が
                # 次の起動まで残り、画面が「実行中」のままになる (2026-09-26 レビュー)
                with contextlib.suppress(Exception):
                    record(step.job_id, "failed", f"chain={chain_id} 中断")
                raise
            if status == "failed":
                failed.append(step.job_id)
                _log.warning(
                    "job_chain_step_failed", chain=chain_id, step=step.job_id, error=detail
                )
            else:
                _log.info(
                    "job_chain_step_done", chain=chain_id, step=step.job_id, elapsed=round(elapsed)
                )
            record(step.job_id, status, f"chain={chain_id} elapsed={elapsed:.0f}s {detail}".strip())
    total = time.monotonic() - started
    _log.info(
        "job_chain_done", chain=chain_id, failed=failed, skipped=skipped, elapsed=round(total)
    )
    return ChainResult(
        chain_id=chain_id,
        failed=tuple(failed),
        elapsed_seconds=total,
        skipped=tuple(skipped),
    )


async def _run_step(step: ChainStep) -> tuple[str, str, float]:
    """1 段を timeout 付きで走らせ (status, detail, 所要秒) を返す。失敗は隔離する。"""
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
    return status, detail, time.monotonic() - t0
