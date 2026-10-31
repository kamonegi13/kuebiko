"""ローカル LLM の step 別使用量の記録 (P0-16)。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast

import httpx
import ollama
import pytest

from src.config_loader import AppConfig
from src.ops import write_failures
from src.ops.local_llm_usage_summary import summarize_local_usage
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import LLMError, OllamaClient
from src.tools.local_usage import (
    LocalUsageBuffer,
    LocalUsageRecord,
    make_local_recorder,
)

_MODEL = "gemma4:26b"


def _rec(step: str | None = "article_summary", **kw: object) -> LocalUsageRecord:
    base: dict[str, object] = {
        "step": step,
        "model": _MODEL,
        "duration_ms": 2000,
        "input_tokens": 100,
        "output_tokens": 50,
        "max_tokens": 512,
        "hit_token_cap": False,
        "structured": True,
        "output_chars": 120,
        "error_class": None,
    }
    base.update(kw)
    return LocalUsageRecord(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_failures() -> None:
    write_failures.reset_for_tests()


def _client(handler: object, recorder: object) -> OllamaClient:
    inner = ollama.AsyncClient(
        host="http://localhost:11434",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    return OllamaClient(
        model=_MODEL,
        client=inner,
        step="article_summary",
        usage_recorder=recorder,  # type: ignore[arg-type]
    )


def _ok(eval_count: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": _MODEL,
            "created_at": "2026-10-29T00:00:00Z",
            "message": {"role": "assistant", "content": "本文"},
            "done": True,
            "prompt_eval_count": 100,
            "eval_count": eval_count,
        },
    )


class TestOllamaClientRecording:
    @pytest.mark.asyncio
    async def test_records_step_tokens_and_cap_flag(self) -> None:
        seen: list[LocalUsageRecord] = []
        client = _client(lambda _r: _ok(64), seen.append)

        await client.generate("問い", max_tokens=64)

        assert len(seen) == 1
        assert seen[0].step == "article_summary"
        assert seen[0].model == _MODEL
        assert seen[0].input_tokens == 100
        assert seen[0].output_tokens == 64
        assert seen[0].hit_token_cap is True
        assert seen[0].structured is False
        assert seen[0].error_class is None

    @pytest.mark.asyncio
    async def test_records_error_class_and_reraises(self) -> None:
        seen: list[LocalUsageRecord] = []
        client = _client(lambda _r: httpx.Response(500, json={"error": "boom"}), seen.append)

        with pytest.raises(LLMError):
            await client.generate("問い")

        assert len(seen) == 1
        assert seen[0].error_class is not None
        assert seen[0].output_tokens == 0

    @pytest.mark.asyncio
    async def test_recorder_failure_does_not_break_the_call_and_is_counted(self) -> None:
        def boom(_r: LocalUsageRecord) -> None:
            raise RuntimeError("db down")

        client = _client(lambda _r: _ok(5), boom)

        resp = await client.generate("問い")

        assert resp.text == "本文"
        assert write_failures.snapshot()["local_llm_usage"] == 1

    @pytest.mark.asyncio
    async def test_no_recorder_means_no_recording(self) -> None:
        inner = ollama.AsyncClient(
            host="http://localhost:11434", transport=httpx.MockTransport(lambda _r: _ok(5))
        )
        client = OllamaClient(model=_MODEL, client=inner)

        assert (await client.generate("問い")).text == "本文"


class TestBuffer:
    def test_flushes_when_batch_size_reached(self) -> None:
        batches: list[Sequence[LocalUsageRecord]] = []
        buf = LocalUsageBuffer(batches.append, flush_size=3, flush_interval=3600)

        for _ in range(7):
            buf.add(_rec())

        assert [len(b) for b in batches] == [3, 3]
        buf.flush()
        assert [len(b) for b in batches] == [3, 3, 1]

    def test_writer_failure_is_counted_not_raised(self) -> None:
        def bad(_b: Sequence[LocalUsageRecord]) -> None:
            raise OSError("disk")

        buf = LocalUsageBuffer(bad, flush_size=1)

        buf.add(_rec())

        assert write_failures.snapshot()["local_llm_usage"] == 1

    def test_overflow_drops_oldest_and_counts(self) -> None:
        batches: list[Sequence[LocalUsageRecord]] = []
        buf = LocalUsageBuffer(batches.append, flush_size=100, flush_interval=3600, max_pending=2)

        for i in range(4):
            buf.add(_rec(duration_ms=i))
        buf.flush()

        assert [r.duration_ms for r in batches[0]] == [2, 3]
        assert write_failures.snapshot()["local_llm_usage"] == 2

    def test_flag_off_records_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        batches: list[Sequence[LocalUsageRecord]] = []
        buf = LocalUsageBuffer(batches.append, flush_size=1)
        monkeypatch.setenv("LLM_USAGE_LOCAL", "0")

        make_local_recorder(buf)(_rec())

        assert batches == []


class TestRepository:
    def test_roundtrip_and_external_summary_is_not_diluted(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "u.db")
        repo.record_llm_usage(
            provider="anthropic",
            model="m",
            input_tokens=10,
            output_tokens=5,
            duration_ms=100,
        )
        repo.record_local_llm_usage_batch(
            [_rec(), _rec(step=None, hit_token_cap=True, error_class="LLMTimeoutError")]
        )

        rows = repo.list_local_llm_usage(days=1)
        summary = repo.llm_usage_summary()

        assert len(rows) == 2
        assert {r["step"] for r in rows} == {"article_summary", None}
        assert any(r["hit_token_cap"] and r["error_class"] == "LLMTimeoutError" for r in rows)
        assert set(summary) == {"anthropic"}

    def test_purge_removes_local_rows(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "u.db")
        repo.record_local_llm_usage_batch([_rec()])

        assert repo.purge_old_llm_usage(retention_days=0) == 1


class TestSummary:
    def test_groups_by_step_and_model_with_percentiles(self) -> None:
        rows = [
            {
                "step": "a",
                "model": "m",
                "duration_ms": d,
                "input_tokens": 10,
                "output_tokens": 5,
                "hit_token_cap": d == 10000,
                "error_class": None,
            }
            for d in (1000, 2000, 3000, 4000, 10000)
        ] + [
            {
                "step": "a",
                "model": "m",
                "duration_ms": 99999,
                "input_tokens": 0,
                "output_tokens": 0,
                "hit_token_cap": False,
                "error_class": "LLMTimeoutError",
            }
        ]

        out = summarize_local_usage(rows, days=2)

        assert len(out) == 1
        g = out[0]
        assert g["calls"] == 6
        assert g["errors"] == 1
        assert g["calls_per_day"] == 3.0
        assert g["avg_seconds"] == 4.0
        assert g["p90_seconds"] == 10.0
        assert g["hit_token_cap_rate"] == 0.2

    def test_missing_step_is_labelled(self) -> None:
        rows = [
            {
                "step": None,
                "model": "m",
                "duration_ms": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "hit_token_cap": False,
                "error_class": None,
            }
        ]

        assert summarize_local_usage(rows, days=1)[0]["step"] == "(unspecified)"


# ---------- 追加 (P0-16 レビュー対応) ----------


class _SpyBuffer(LocalUsageBuffer):
    def __init__(self) -> None:
        super().__init__(lambda _b: None, flush_size=10_000, flush_interval=3600)
        self.seen: list[LocalUsageRecord] = []

    def add(self, record: LocalUsageRecord) -> None:
        self.seen.append(record)


class TestProbeRecording:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.tools.model_tiers import invalidate_model_tiers_cache

        monkeypatch.setenv("MODEL_TIERS_CONFIG_DB", "0")
        monkeypatch.setenv("LLM_USAGE_LOCAL", "1")
        invalidate_model_tiers_cache()

    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        top: list[tuple[str, float]],
        *,
        status: int = 200,
    ) -> list[LocalUsageRecord]:
        import asyncio

        import src.tools.local_usage as lu
        from src.tools.decision_probe import probe_choice
        from src.tools.model_tiers import Step

        spy = _SpyBuffer()
        monkeypatch.setattr(lu, "_BUFFER", spy)
        monkeypatch.setattr(lu, "_install_sigterm_flush", lambda _b: None)

        def handler(_r: httpx.Request) -> httpx.Response:
            if status != 200:
                return httpx.Response(status, json={"error": "x"})
            return httpx.Response(
                200,
                json={
                    "model": _MODEL,
                    "created_at": "2026-10-29T00:00:00Z",
                    "message": {"role": "assistant", "content": "Yes"},
                    "done": True,
                    "prompt_eval_count": 40,
                    "eval_count": 1,
                    "load_duration": 3_000_000,
                    "prompt_eval_duration": 40_000_000,
                    "eval_duration": 10_000_000,
                    "logprobs": [
                        {
                            "token": top[0][0],
                            "logprob": top[0][1],
                            "top_logprobs": [{"token": t, "logprob": lp} for t, lp in top],
                        }
                    ],
                },
            )

        client = ollama.AsyncClient(
            host="http://localhost:11434", transport=httpx.MockTransport(handler)
        )

        class _Cfg:
            ollama_base_url = "http://localhost:11434"

        asyncio.run(
            probe_choice(
                Step.TRIAGE,
                "q",
                ("Yes", "No"),
                config=cast(AppConfig, _Cfg()),
                client=client,
            )
        )
        return spy.seen

    def test_both_choices_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._run(monkeypatch, [("Yes", -0.1), ("No", -2.5)])

        assert len(seen) == 1
        assert seen[0].provider == "ollama-probe"
        assert seen[0].choices_present is True
        assert seen[0].floor_used is False
        assert seen[0].input_tokens == 40
        assert (seen[0].load_ms, seen[0].prompt_eval_ms, seen[0].eval_ms) == (3, 40, 10)

    def test_one_choice_missing_uses_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._run(monkeypatch, [("Yes", -0.1), ("maybe", -3.0)])

        assert seen[0].choices_present is False
        assert seen[0].floor_used is True

    def test_error_is_recorded_with_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._run(monkeypatch, [("Yes", -0.1)], status=500)

        assert len(seen) == 1
        assert seen[0].error_class == "ResponseError"


class TestFlushBehaviour:
    @pytest.mark.asyncio
    async def test_flush_inside_event_loop_runs_in_another_thread(self) -> None:
        import asyncio
        import threading

        threads: list[str] = []
        done = threading.Event()

        def writer(_b: Sequence[LocalUsageRecord]) -> None:
            threads.append(threading.current_thread().name)
            done.set()

        buf = LocalUsageBuffer(writer, flush_size=1)

        buf.add(_rec())
        for _ in range(100):
            if done.is_set():
                break
            await asyncio.sleep(0.01)

        assert threads
        assert threads[0] != threading.main_thread().name

    def test_defaults_are_ten_records_or_ten_seconds(self) -> None:
        from src.tools.local_usage import FLUSH_INTERVAL_SECONDS, FLUSH_SIZE

        assert (FLUSH_SIZE, FLUSH_INTERVAL_SECONDS) == (10, 10.0)

    def test_sigterm_flushes_buffer_then_chains_previous_handler(self) -> None:
        import signal

        import src.tools.local_usage as lu

        chained: list[int] = []
        original = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda signum, _f: chained.append(signum))
        batches: list[Sequence[LocalUsageRecord]] = []
        buf = LocalUsageBuffer(batches.append, flush_size=100, flush_interval=3600)
        lu._sigterm_installed = False  # noqa: SLF001
        try:
            lu._install_sigterm_flush(buf)  # noqa: SLF001
            buf.add(_rec())
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
        finally:
            signal.signal(signal.SIGTERM, original)
            lu._sigterm_installed = False  # noqa: SLF001

        assert [len(b) for b in batches] == [1]
        assert chained == [signal.SIGTERM]

    def test_recorder_respects_flag_default_off_in_tests(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        assert os.environ["LLM_USAGE_LOCAL"] == "0"  # tests/conftest.py
        batches: list[Sequence[LocalUsageRecord]] = []
        buf = LocalUsageBuffer(batches.append, flush_size=1)
        make_local_recorder(buf)(_rec())
        assert batches == []
        monkeypatch.setenv("LLM_USAGE_LOCAL", "1")
        make_local_recorder(buf)(_rec())
        assert len(batches) == 1


class TestFallbackAndProbeColumns:
    def test_roundtrip_new_columns_and_summary(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "p.db")
        repo.record_local_llm_usage_batch(
            [
                _rec(fallback=True),
                _rec(
                    step="triage",
                    provider="ollama-probe",
                    choices_present=False,
                    floor_used=True,
                ),
                _rec(
                    step="triage",
                    provider="ollama-probe",
                    choices_present=False,
                    floor_used=False,
                ),
            ]
        )

        rows = repo.list_local_llm_usage(days=1)
        out = {(g["provider"], g["step"]): g for g in summarize_local_usage(rows, days=1)}

        assert out[("ollama", "article_summary")]["fallback_calls"] == 1
        probe = out[("ollama-probe", "triage")]
        assert probe["floor_used_rate"] == 0.5
        assert probe["choices_missing_rate"] == 0.5
        assert out[("ollama", "article_summary")]["floor_used_rate"] is None
        assert set(repo.llm_usage_summary()) == set()


class TestShutdownFlush:
    def test_hung_writer_is_abandoned_after_timeout_and_counted(self) -> None:
        import threading
        import time

        release = threading.Event()

        def hung(_b: Sequence[LocalUsageRecord]) -> None:
            release.wait(5)

        buf = LocalUsageBuffer(hung, flush_size=100, flush_interval=3600)
        buf.add(_rec())
        started = time.monotonic()

        buf.flush_for_shutdown(timeout=0.2)

        elapsed = time.monotonic() - started
        release.set()
        for _ in range(100):
            if write_failures.snapshot().get("local_llm_usage"):
                break
            time.sleep(0.01)
        assert elapsed < 1.0
        assert write_failures.snapshot()["local_llm_usage"] == 1

    def test_writes_normally_within_timeout(self) -> None:
        batches: list[Sequence[LocalUsageRecord]] = []
        buf = LocalUsageBuffer(batches.append, flush_size=100, flush_interval=3600)
        buf.add(_rec())

        buf.flush_for_shutdown(timeout=1.0)

        assert [len(b) for b in batches] == [1]
        assert write_failures.snapshot().get("local_llm_usage", 0) == 0
