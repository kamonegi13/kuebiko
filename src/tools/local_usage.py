"""ローカル (Ollama) LLM 呼び出しの step 別使用量の記録 (P0-16)。

目的は容量の計画 (s26: step ごとの 回/日 × 秒/回、``hit_token_cap`` の率)。
プロンプト・応答の本文は一切持たない (件数・時間・トークン・フラグ・例外の型だけ)。

熱い経路 (``OllamaClient._chat``) から呼ばれるので、**1 件ごとに DB を触らず**
バッファして ``FLUSH_SIZE`` 件または ``FLUSH_INTERVAL_SECONDS`` ごとにまとめて書く。
記録の失敗は ``write_failures`` に数え、LLM 呼び出しには絶対に伝えない。
旗 ``LLM_USAGE_LOCAL`` (既定 1、0 で記録を止める)。
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from src.ops.write_failures import record_failure

LOCAL_PROVIDER = "ollama"
FLUSH_SIZE = 50
FLUSH_INTERVAL_SECONDS = 60.0
# 書き込みが連続して失敗しても無限に溜めない (古い方を捨てて数える)
MAX_PENDING = 2000
LLM_USAGE_LOCAL_ENV = "LLM_USAGE_LOCAL"
_OFF_VALUES = ("0", "false", "off", "")


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
        self._lock = threading.Lock()
        self._pending: list[LocalUsageRecord] = []
        self._last_flush = time.monotonic()

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
            self.flush()

    def flush(self) -> None:
        with self._lock:
            batch, self._pending = self._pending, []
            self._last_flush = time.monotonic()
        if not batch:
            return
        try:
            self._writer(batch)
        except Exception as e:  # noqa: BLE001 — 記録の失敗で LLM 呼び出しを壊さない
            record_failure("local_llm_usage", e)


_BUFFER = LocalUsageBuffer()
atexit.register(_BUFFER.flush)


def make_local_recorder(buffer: LocalUsageBuffer | None = None) -> LocalUsageRecorder:
    """``OllamaClient`` に渡す recorder。旗 0 なら何もしない。"""
    target = buffer or _BUFFER

    def _record(record: LocalUsageRecord) -> None:
        if local_usage_enabled():
            target.add(record)

    return _record
