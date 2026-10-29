"""ローカル (Ollama) LLM 呼び出しの step 別使用量の記録 (P0-16)。

目的は容量の計画 (s26: step ごとの 回/日 × 秒/回、``hit_token_cap`` の率)。
プロンプト・応答の本文は一切持たない (件数・時間・トークン・フラグ・例外の型だけ)。

熱い経路 (``OllamaClient._chat`` / ``decision_probe.probe_choice``) から呼ばれるので、
**1 件ごとに DB を触らず**バッファして ``FLUSH_SIZE`` 件または ``FLUSH_INTERVAL_SECONDS``
ごとにまとめて書く。書き込みは event loop の中なら別スレッドへ逃がす。
子プロセス (パイプライン) が SIGTERM で止められたときもバッファを書き出す。
記録の失敗は ``write_failures`` に数え、LLM 呼び出しには絶対に伝えない。

旗 ``LLM_USAGE_LOCAL`` (既定 1、0 で記録を止める)。**評価・教師収穫のスクリプトと
テストは 0 にして走らせる** (本番の容量の統計に混ぜない。tests は conftest で 0)。

未実装: ``queue_wait_ms`` (同じモデルへの待ち行列)。Ollama は待ち行列の時間を返さず、
クライアント側にも待ち行列が無いため、測れる形にならない (P0-16 の注記)。
"""

from __future__ import annotations

import asyncio
import atexit
import os
import signal
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import FrameType

from src.ops.write_failures import record_failure

LOCAL_PROVIDER = "ollama"
PROBE_PROVIDER = "ollama-probe"
FLUSH_SIZE = 10
FLUSH_INTERVAL_SECONDS = 10.0
# 書き込みが連続して失敗しても無限に溜めない (古い方を捨てて数える)
MAX_PENDING = 2000
LLM_USAGE_LOCAL_ENV = "LLM_USAGE_LOCAL"
_OFF_VALUES = ("0", "false", "off", "")
_MS_PER_SECOND = 1000
# SIGTERM で DB に書く最大の待ち (超えたら捨てて正常終了を優先する)
SHUTDOWN_FLUSH_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class LocalUsageRecord:
    """ローカル LLM 呼び出し 1 回分。"""

    step: str | None
    model: str
    duration_ms: int
    input_tokens: int
    output_tokens: int
    max_tokens: int
    hit_token_cap: bool
    structured: bool
    output_chars: int
    error_class: str | None = None
    provider: str = LOCAL_PROVIDER
    # logprob の経路だけ: 全選択肢が top_logprobs に在ったか / 欠けて floor で補ったか
    choices_present: bool | None = None
    floor_used: bool | None = None
    # 外部 → ローカルの受け皿 (緊急の受け皿を含む) に落ちた呼び出しか
    fallback: bool = False


LocalUsageRecorder = Callable[[LocalUsageRecord], None]
BatchWriter = Callable[[Sequence[LocalUsageRecord]], None]


def local_usage_enabled() -> bool:
    return os.environ.get(LLM_USAGE_LOCAL_ENV, "1").strip().lower() not in _OFF_VALUES


def _default_writer(batch: Sequence[LocalUsageRecord]) -> None:
    from src.storage.run_history import RunHistoryRepository  # 遅延 import (循環回避)

    RunHistoryRepository().record_local_llm_usage_batch(batch)


class LocalUsageBuffer:
    """スレッド安全なバッファ。``add`` / ``flush`` は例外を上げない。"""

    def __init__(
        self,
        writer: BatchWriter = _default_writer,
        *,
        flush_size: int = FLUSH_SIZE,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
        max_pending: int = MAX_PENDING,
    ) -> None:
        self._writer = writer
        self._flush_size = flush_size
        self._flush_interval = flush_interval
        self._max_pending = max_pending
        # シグナルハンドラ (同じスレッド) からの flush が add の最中に入っても詰まらないよう RLock
        self._lock = threading.RLock()
        self._pending: list[LocalUsageRecord] = []
        self._last_flush = time.monotonic()
        self._inflight: set[asyncio.Future[None]] = set()

    def add(self, record: LocalUsageRecord) -> None:
        overflowed = False
        with self._lock:
            self._pending = [*self._pending, record]
            if len(self._pending) > self._max_pending:
                self._pending = self._pending[-self._max_pending :]
                overflowed = True
            due = (
                len(self._pending) >= self._flush_size
                or time.monotonic() - self._last_flush >= self._flush_interval
            )
        if overflowed:
            record_failure("local_llm_usage", OverflowError("buffer overflow"))
        if due:
            self._flush_async_if_possible()

    def _drain(self) -> list[LocalUsageRecord]:
        with self._lock:
            batch, self._pending = self._pending, []
            self._last_flush = time.monotonic()
        return batch

    def _write(self, batch: Sequence[LocalUsageRecord]) -> None:
        if not batch:
            return
        try:
            self._writer(batch)
        except Exception as e:  # noqa: BLE001 — 記録の失敗で LLM 呼び出しを壊さない
            record_failure("local_llm_usage", e)

    def _flush_async_if_possible(self) -> None:
        """event loop の中なら書き込みだけを別スレッドへ (loop を DB で塞がない)。"""
        batch = self._drain()
        if not batch:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write(batch)
            return
        future = loop.run_in_executor(None, self._write, batch)
        self._inflight.add(future)
        future.add_done_callback(self._inflight.discard)

    def flush(self) -> None:
        """同期で書き出す (atexit・テスト用)。シグナルハンドラからは呼ばない。"""
        self._write(self._drain())

    def flush_for_shutdown(self, timeout: float = SHUTDOWN_FLUSH_TIMEOUT_SECONDS) -> None:
        """SIGTERM 用: 書き込みを別スレッドに渡し、``timeout`` 秒で見切る。

        DB が止まっていても正常終了を遅らせない。見切った分と、別スレッドで書きかけの
        バッチは捨て、件数だけ ``write_failures`` に数える (シグナルハンドラの中では
        ロックを取る処理を直接しない)。
        """
        batch = self._drain()
        lost_inflight = sum(1 for f in list(self._inflight) if not f.done())
        if lost_inflight:
            _count_lost_async(lost_inflight)
        if not batch:
            return
        worker = threading.Thread(target=self._write, args=(batch,), daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            _count_lost_async(1)


def _count_lost_async(count: int) -> None:
    """捨てたバッチの件数を数える。ハンドラ (主スレッド) がロックで詰まらないよう別スレッドで。"""

    def _count() -> None:
        for _ in range(count):
            record_failure("local_llm_usage", TimeoutError("dropped at shutdown"))

    threading.Thread(target=_count, daemon=True).start()


_BUFFER = LocalUsageBuffer()
atexit.register(_BUFFER.flush)
_sigterm_installed = False


def _install_sigterm_flush(buffer: LocalUsageBuffer) -> None:
    """SIGTERM で止められたときにバッファを書き出してから元の挙動に戻す (1 回だけ)。

    元のハンドラ (uvicorn 等) があればそれを呼び、無ければ既定動作で終了する。
    メインスレッド以外・シグナル非対応の環境では何もしない。
    """
    global _sigterm_installed  # noqa: PLW0603
    if _sigterm_installed:
        return
    _sigterm_installed = True
    try:
        previous = signal.getsignal(signal.SIGTERM)

        def _handler(signum: int, frame: FrameType | None) -> None:
            buffer.flush_for_shutdown()
            if callable(previous):
                previous(signum, frame)
            elif previous == signal.SIG_DFL:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGTERM)

        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):  # メインスレッドでない等
        _sigterm_installed = False


def make_local_recorder(buffer: LocalUsageBuffer | None = None) -> LocalUsageRecorder:
    """``OllamaClient`` に渡す recorder。旗 0 なら何もしない。"""
    target = buffer or _BUFFER

    def _record(record: LocalUsageRecord) -> None:
        if not local_usage_enabled():
            return
        if target is _BUFFER:
            _install_sigterm_flush(target)
        target.add(record)

    return _record


def record_probe_usage(
    *,
    step: str,
    model: str,
    started_monotonic: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    choices_present: bool | None = None,
    floor_used: bool | None = None,
    error_class: str | None = None,
) -> None:
    """logprob 判定 (``decision_probe``) 1 回分を記録する。fail-safe。"""
    try:
        make_local_recorder()(
            LocalUsageRecord(
                step=step,
                model=model,
                duration_ms=int((time.monotonic() - started_monotonic) * _MS_PER_SECOND),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                max_tokens=1,
                hit_token_cap=False,
                structured=False,
                output_chars=0,
                error_class=error_class,
                provider=PROBE_PROVIDER,
                choices_present=choices_present,
                floor_used=floor_used,
            )
        )
    except Exception as e:  # noqa: BLE001 — 記録の失敗で判定を壊さない
        record_failure("local_llm_usage", e)
