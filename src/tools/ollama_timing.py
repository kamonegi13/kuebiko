"""Ollama 応答の所要時間の内訳 (load / prefill / decode) の抽出。

Ollama の /api/chat・/api/generate の応答は ``load_duration`` /
``prompt_eval_duration`` / ``eval_duration`` / ``total_duration`` を ns で返す。
prefill (prompt_eval) と decode (eval) を分けて、ホスト側の速度低下を診断する。
欠けている欄 (ストリーミングの途中・古い版・エラー) は ``None`` のまま扱い、0 に化けさせない。
本文は一切持たない (秒と比率だけ)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_NS_PER_SECOND = 1_000_000_000.0
_NS_PER_MS = 1_000_000
_SECONDS_DIGITS = 3
_RATE_DIGITS = 1


@dataclass(frozen=True)
class OllamaTiming:
    """1 応答分の時間の内訳 (ms)。欠けは None。"""

    load_ms: int | None = None
    prompt_eval_ms: int | None = None
    eval_ms: int | None = None
    total_ms: int | None = None


def _ns_to_ms(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 0:
        return None
    return int(value) // _NS_PER_MS


def _get(obj: object, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def extract_timing(response: object) -> OllamaTiming:
    """応答 (SDK オブジェクト / dict) から時間の内訳を取り出す。欠けは None。"""
    return OllamaTiming(
        load_ms=_ns_to_ms(_get(response, "load_duration")),
        prompt_eval_ms=_ns_to_ms(_get(response, "prompt_eval_duration")),
        eval_ms=_ns_to_ms(_get(response, "eval_duration")),
        total_ms=_ns_to_ms(_get(response, "total_duration")),
    )


def tokens_per_second(tokens: int, elapsed_ms: int | None) -> float | None:
    """tok/s。時間が欠け・0、トークンが 0 以下なら None (測れていない)。"""
    if elapsed_ms is None or elapsed_ms <= 0 or tokens <= 0:
        return None
    return tokens / (elapsed_ms / 1000.0)


def timing_log_fields(
    timing: OllamaTiming, *, input_tokens: int, output_tokens: int
) -> dict[str, Any]:
    """``ollama_response`` ログに足す欄 (欠けは None)。"""

    def _seconds(ms: int | None) -> float | None:
        return None if ms is None else round(ms / 1000.0, _SECONDS_DIGITS)

    def _rate(tokens: int, ms: int | None) -> float | None:
        rate = tokens_per_second(tokens, ms)
        return None if rate is None else round(rate, _RATE_DIGITS)

    return {
        "load_s": _seconds(timing.load_ms),
        "prompt_eval_s": _seconds(timing.prompt_eval_ms),
        "eval_s": _seconds(timing.eval_ms),
        "prompt_tok_s": _rate(input_tokens, timing.prompt_eval_ms),
        "eval_tok_s": _rate(output_tokens, timing.eval_ms),
    }
