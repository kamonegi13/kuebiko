"""LLM の出力が上限に当たったかを呼び出し側へ渡す (2026-11-20、docs/synthesis_length_gate.md §2.1)。

上限に当たった構造化出力は ``OllamaClient.generate_structured`` の救済で括弧を閉じられ、
途中までの JSON が検証を通る。クライアントは到達を知っている (``eval_count >= max_tokens``) が、
抽象の署名を変えずに呼び出し側へ渡すため、ContextVar に置いた入れ物へ印を付ける。
包み (task_prefix・fallback) は素通しで、``asyncio.wait_for`` の子 task にも同じ入れ物が写る。
外部クライアントは印を付けない (付かなければ呼び出し側は従来の推測に倒す)。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class TruncationProbe:
    """1 回の見張りの結果 (見張りの間だけ書き換わる入れ物)。"""

    hit_cap: bool = False
    salvaged: bool = False
    #: 印を付けられるクライアントが応答を返したか (False = 外部など、信号が無い)
    observed: bool = False

    @property
    def truncated(self) -> bool:
        return self.hit_cap or self.salvaged


_PROBE: ContextVar[TruncationProbe | None] = ContextVar("llm_truncation_probe", default=None)


@contextmanager
def watch_truncation() -> Iterator[TruncationProbe]:
    probe = TruncationProbe()
    token = _PROBE.set(probe)
    try:
        yield probe
    finally:
        _PROBE.reset(token)


def note_observed() -> None:
    probe = _PROBE.get()
    if probe is not None:
        probe.observed = True


def note_hit_cap() -> None:
    probe = _PROBE.get()
    if probe is not None:
        probe.hit_cap = True


def note_salvaged() -> None:
    probe = _PROBE.get()
    if probe is not None:
        probe.salvaged = True


__all__ = [
    "TruncationProbe",
    "note_hit_cap",
    "note_observed",
    "note_salvaged",
    "watch_truncation",
]
