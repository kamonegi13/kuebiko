"""Ollama 応答の時間の内訳 (load / prefill / decode) の抽出・ログ・記録・集計。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import ollama
import pytest

from src.ops.local_llm_usage_summary import summarize_local_usage
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import OllamaClient
from src.tools.local_usage import LocalUsageRecord
from src.tools.ollama_timing import (
    OllamaTiming,
    extract_timing,
    timing_log_fields,
    tokens_per_second,
)

_MODEL = "gemma4:26b"
_NS = 1_000_000_000
_SECRET = "SECRET-PROMPT-BODY"


def _payload(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": _MODEL,
        "created_at": "2026-10-30T00:00:00Z",
        "message": {"role": "assistant", "content": "応答本文"},
        "done": True,
        "prompt_eval_count": 200,
        "eval_count": 100,
    }
    base.update(extra)
    return base


_TIMING = {
    "load_duration": 2 * _NS,
    "prompt_eval_duration": 4 * _NS,
    "eval_duration": 5 * _NS,
    "total_duration": 11 * _NS,
}


class TestExtract:
    def test_reads_all_fields_from_dict(self) -> None:
        t = extract_timing(_TIMING)
        assert t == OllamaTiming(load_ms=2000, prompt_eval_ms=4000, eval_ms=5000, total_ms=11000)

    def test_missing_fields_are_none_not_zero(self) -> None:
        assert extract_timing({"eval_duration": None}) == OllamaTiming()
        assert extract_timing(object()) == OllamaTiming()

    def test_rejects_negative_and_non_numeric(self) -> None:
        t = extract_timing({"eval_duration": -1, "load_duration": "x", "total_duration": True})
        assert t == OllamaTiming()

    def test_tokens_per_second_guards(self) -> None:
        assert tokens_per_second(100, 5000) == 20.0
        assert tokens_per_second(100, None) is None
        assert tokens_per_second(100, 0) is None
        assert tokens_per_second(0, 5000) is None

    def test_log_fields(self) -> None:
        f = timing_log_fields(extract_timing(_TIMING), input_tokens=200, output_tokens=100)
        assert f == {
            "load_s": 2.0,
            "prompt_eval_s": 4.0,
            "eval_s": 5.0,
            "prompt_tok_s": 50.0,
            "eval_tok_s": 20.0,
        }
        empty = timing_log_fields(OllamaTiming(), input_tokens=1, output_tokens=1)
        assert set(empty.values()) == {None}


def _client(payload: dict[str, Any], seen: list[LocalUsageRecord]) -> OllamaClient:
    inner = ollama.AsyncClient(
        host="http://localhost:11434",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload)),
    )
    return OllamaClient(
        model=_MODEL, client=inner, step="article_summary", usage_recorder=seen.append
    )


class _LogSpy:
    """llm_client._log の差し替え (他テストの structlog 設定・キャッシュに依存しない)。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def _add(self, event: str, **kw: Any) -> None:
        self.events.append({"event": event, **kw})

    info = warning = error = debug = _add


@pytest.fixture
def log_spy(monkeypatch: pytest.MonkeyPatch) -> _LogSpy:
    import src.tools.llm_client as lc

    spy = _LogSpy()
    monkeypatch.setattr(lc, "_log", spy)
    return spy


class TestClientLogAndRecord:
    @pytest.mark.asyncio
    async def test_log_and_record_carry_timing_without_prompt_text(self, log_spy: _LogSpy) -> None:
        seen: list[LocalUsageRecord] = []
        client = _client(_payload(**_TIMING), seen)

        await client.generate(_SECRET)
        logs = log_spy.events

        entry = next(e for e in logs if e["event"] == "ollama_response")
        assert entry["load_s"] == 2.0
        assert entry["prompt_eval_s"] == 4.0
        assert entry["eval_s"] == 5.0
        assert entry["prompt_tok_s"] == 50.0
        assert entry["eval_tok_s"] == 20.0
        assert entry["input_tokens"] == 200
        assert entry["output_tokens"] == 100
        assert _SECRET not in repr(logs)
        assert "応答本文" not in repr(logs)
        assert (seen[0].load_ms, seen[0].prompt_eval_ms, seen[0].eval_ms) == (2000, 4000, 5000)

    @pytest.mark.asyncio
    async def test_missing_fields_log_none_and_record_none(self, log_spy: _LogSpy) -> None:
        seen: list[LocalUsageRecord] = []
        client = _client(_payload(), seen)

        resp = await client.generate("問い")
        logs = log_spy.events

        entry = next(e for e in logs if e["event"] == "ollama_response")
        assert entry["eval_tok_s"] is None and entry["load_s"] is None
        assert (seen[0].load_ms, seen[0].prompt_eval_ms, seen[0].eval_ms) == (None, None, None)
        assert resp.text == "応答本文"


def _rec(**kw: Any) -> LocalUsageRecord:
    base: dict[str, Any] = {
        "step": "article_summary",
        "model": _MODEL,
        "duration_ms": 2000,
        "input_tokens": 200,
        "output_tokens": 100,
        "max_tokens": 512,
        "hit_token_cap": False,
        "structured": False,
        "output_chars": 10,
    }
    base.update(kw)
    return LocalUsageRecord(**base)


class TestDbAndSummary:
    def test_roundtrip_and_median_rates(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "t.db")
        repo.record_local_llm_usage_batch(
            [
                _rec(load_ms=1000, prompt_eval_ms=4000, eval_ms=5000),  # 50 / 20 tok/s
                _rec(load_ms=3000, prompt_eval_ms=2000, eval_ms=2500),  # 100 / 40
                _rec(load_ms=5000, prompt_eval_ms=1000, eval_ms=1000),  # 200 / 100
                _rec(),  # 欠け: 中央値に混ぜない
            ]
        )

        rows = repo.list_local_llm_usage(days=1)
        assert sum(1 for r in rows if r["eval_ms"] is None) == 1
        (g,) = summarize_local_usage(rows, days=1)

        assert g["median_load_s"] == 3.0
        assert g["median_prompt_tok_s"] == 100.0
        assert g["median_eval_tok_s"] == 40.0
        assert g["timing_rows"] == 3

    def test_summary_none_when_no_timing(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "t.db")
        repo.record_local_llm_usage_batch([_rec()])
        (g,) = summarize_local_usage(repo.list_local_llm_usage(days=1), days=1)
        assert g["median_eval_tok_s"] is None
        assert g["median_load_s"] is None
        assert g["timing_rows"] == 0


class TestSummaryApi:
    def test_endpoint_exposes_median_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from src.ui.api.pages import local_llm_usage_summary

        repo = RunHistoryRepository(db_path=tmp_path / "t.db")
        repo.record_local_llm_usage_batch([_rec(load_ms=1000, prompt_eval_ms=4000, eval_ms=5000)])
        request: Any = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))

        out = local_llm_usage_summary(request, days=1)

        assert out["by_step"][0]["median_eval_tok_s"] == 20.0
        assert out["by_step"][0]["median_prompt_tok_s"] == 50.0
