"""ローカル LLM 使用量の step 別集計 (P0-16、容量の計画用の純関数)。"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from src.tools.ollama_timing import tokens_per_second

UNSPECIFIED_STEP = "(unspecified)"
_P90 = 0.9
_MS_PER_SECOND = 1000.0
_RATE_DIGITS = 4
_SECONDS_DIGITS = 3
_TOK_S_DIGITS = 1


def _percentile(sorted_values: list[int], q: float) -> int:
    """最近順位法の分位点 (空は 0)。"""
    if not sorted_values:
        return 0
    idx = max(0, math.ceil(q * len(sorted_values)) - 1)
    return sorted_values[idx]


def summarize_local_usage(rows: Iterable[Mapping[str, Any]], *, days: int) -> list[dict[str, Any]]:
    """(step, model) ごとの 回/日・秒/回 (平均・p90・最大)・トークン・暴走率・失敗数。

    呼び出し回数の多い順。エラー行は回数・失敗数に数えるが、時間・トークンの
    統計は成功した呼び出しだけで取る (失敗は早く終わるか timeout で歪めるため)。
    """
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        key = (
            str(r.get("provider") or "ollama"),
            str(r.get("step") or UNSPECIFIED_STEP),
            str(r.get("model") or ""),
        )
        groups[key].append(r)
    out: list[dict[str, Any]] = []
    for (provider, step, model), items in groups.items():
        ok = [r for r in items if not r.get("error_class")]
        durations = sorted(int(r["duration_ms"]) for r in ok)
        calls = len(items)
        out.append(
            {
                "provider": provider,
                "step": step,
                "model": model,
                "calls": calls,
                "calls_per_day": round(calls / max(1, days), 1),
                "errors": calls - len(ok),
                "avg_seconds": round(sum(durations) / len(durations) / _MS_PER_SECOND, 2)
                if durations
                else 0.0,
                "p90_seconds": round(_percentile(durations, _P90) / _MS_PER_SECOND, 2),
                "max_seconds": round((durations[-1] if durations else 0) / _MS_PER_SECOND, 2),
                "input_tokens": sum(int(r["input_tokens"]) for r in ok),
                "output_tokens": sum(int(r["output_tokens"]) for r in ok),
                "hit_token_cap_rate": round(
                    sum(1 for r in ok if r.get("hit_token_cap")) / len(ok), _RATE_DIGITS
                )
                if ok
                else 0.0,
                "fallback_calls": sum(1 for r in items if r.get("fallback")),
                # logprob 経路: floor で補った率 / 選択肢が top_logprobs に無く判定できなかった率
                "floor_used_rate": _flag_rate(ok, "floor_used"),
                "choices_missing_rate": _missing_rate(ok),
                # Ollama の時間の内訳 (中央値)。欠けた行は除く。測れた行が無ければ None
                "median_load_s": _median_load_s(ok),
                "median_prompt_tok_s": _median_rate(ok, "input_tokens", "prompt_eval_ms"),
                "median_eval_tok_s": _median_rate(ok, "output_tokens", "eval_ms"),
                "timing_rows": sum(1 for r in ok if r.get("eval_ms") is not None),
            }
        )
    return sorted(out, key=lambda g: (-int(g["calls"]), str(g["step"])))


def _probe_rows(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [r for r in rows if r.get("choices_present") is not None]


def _flag_rate(rows: list[Mapping[str, Any]], key: str) -> float | None:
    """logprob 経路の行だけで key が真の率 (logprob 経路でなければ None)。"""
    probes = _probe_rows(rows)
    if not probes:
        return None
    return round(sum(1 for r in probes if r.get(key)) / len(probes), _RATE_DIGITS)


def _missing_rate(rows: list[Mapping[str, Any]]) -> float | None:
    """選択肢が 1 つも見つからなかった (choices_present も floor_used も偽) 率。"""
    probes = _probe_rows(rows)
    if not probes:
        return None
    missing = sum(1 for r in probes if not r.get("choices_present") and not r.get("floor_used"))
    return round(missing / len(probes), _RATE_DIGITS)


def _median_load_s(rows: list[Mapping[str, Any]]) -> float | None:
    values = [int(r["load_ms"]) for r in rows if r.get("load_ms") is not None]
    if not values:
        return None
    return round(statistics.median(values) / _MS_PER_SECOND, _SECONDS_DIGITS)


def _median_rate(rows: list[Mapping[str, Any]], tokens_key: str, ms_key: str) -> float | None:
    """tok/s の中央値 (時間かトークンが 0 以下の行は測れていないので除く)。"""
    rates = [
        tokens_per_second(int(r.get(tokens_key) or 0), int(r[ms_key]))
        for r in rows
        if r.get(ms_key) is not None
    ]
    valid = [x for x in rates if x is not None]
    if not valid:
        return None
    return round(statistics.median(valid), _TOK_S_DIGITS)
