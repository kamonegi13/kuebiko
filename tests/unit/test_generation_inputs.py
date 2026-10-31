"""生成物の入力の記録 (P0-18) のテスト。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from src.ops import generation_inputs, write_failures
from src.storage.run_history import RunHistoryRepository

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import export_public_site  # type: ignore[import-not-found]  # noqa: E402
from export_mirror import _final_gate  # noqa: E402

PROMPT = "共通の prompt 本文 CVE-2026-0001"


@pytest.fixture(autouse=True)
def _reset() -> None:
    write_failures.reset_for_tests()


def _rows(repo: RunHistoryRepository) -> list[Any]:
    with repo._connect() as conn:
        return list(conn.execute("SELECT * FROM generation_inputs").fetchall())


def _record(repo: Any) -> None:
    generation_inputs.record_generation_input(
        step="eventnews",
        model="m",
        product_kind="eventnews",
        product_id="item1:v1",
        prompt_text=PROMPT,
        repo=repo,
    )


def test_flag_on_records_one_row_with_correct_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GENERATION_INPUT_RECORD", raising=False)
    repo = RunHistoryRepository(db_path=tmp_path / "g.db")
    _record(repo)
    rows = _rows(repo)
    assert len(rows) == 1
    assert rows[0]["prompt_sha256"] == hashlib.sha256(PROMPT.encode()).hexdigest()
    assert rows[0]["prompt_text"] == PROMPT
    assert rows[0]["product_id"] == "item1:v1"
    assert repo.generation_inputs_counts(days=1) == {"eventnews": 1}


def test_flag_off_records_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENERATION_INPUT_RECORD", "0")
    repo = RunHistoryRepository(db_path=tmp_path / "g.db")
    _record(repo)
    assert _rows(repo) == []


def test_write_failure_is_counted_and_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GENERATION_INPUT_RECORD", raising=False)

    class Broken:
        def record_generation_input(self, **_: Any) -> None:
            raise RuntimeError("db down")

    _record(Broken())
    assert write_failures.snapshot()["generation_inputs"] == 1


def test_public_site_forbidden_keys_include_prompt_text() -> None:
    assert "prompt_text" in export_public_site._FORBIDDEN_KEYS


def test_mirror_final_gate_rejects_prompt_text(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text(json.dumps({"items": [{"prompt_text": "secret body"}]}))
    with pytest.raises(SystemExit):
        _final_gate(tmp_path)


# --- 生成サイト (grounded synthesis の render / deep dive) -------------------------------------


class _Spy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self.calls

        def record(_self: Any, **kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(RunHistoryRepository, "record_generation_input", record)


def _raiser(_self: Any, **_: Any) -> None:
    raise RuntimeError("db down")


async def _run_render() -> Any:
    from src.synthesis.grounded.render import render_sections
    from tests.unit.test_synthesis_cot_notes import RecordingLLM, _estimate

    return await render_sections(llm=RecordingLLM(), est=_estimate(), period_label="L")  # type: ignore[arg-type]


async def _run_digest(monkeypatch: pytest.MonkeyPatch) -> str:
    from src.digest import llm_digest as mod
    from src.digest.db_filter import DigestCandidate
    from src.digest.recap_render import RecapOutput, RecapSection

    async def fake(llm: Any, prompt: str, *, max_tokens: int, think: Any) -> RecapOutput:
        return RecapOutput(
            sections=[RecapSection(heading="節", body="あ" * 600, article_ids=["a1"])]
        )

    monkeypatch.setattr(mod, "_generate_sections", fake)
    monkeypatch.setattr(mod, "_render_prompt", lambda *a, **k: "DIGEST PROMPT")
    cand = DigestCandidate(
        article_id="a1",
        title="題",
        url="https://kuebiko.example/a1",
        feed_title="Feed",
        summary="要約",
        importance="high",
        category="apt",
        posted_channel="watch",
        dedup_key=None,
        created_at="2026-09-20T00:00:00Z",
        discord_channel_id=None,
        discord_message_id=None,
    )
    return await mod.generate_digest(
        llm=object(),  # type: ignore[arg-type]
        candidates=[cand],
        template_name="digest/weekly_recap.j2",
        period_label="P",
    )


@pytest.mark.asyncio
async def test_grounded_render_records_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GENERATION_INPUT_RECORD", raising=False)
    spy = _Spy()
    spy.install(monkeypatch)
    await _run_render()
    assert len(spy.calls) == 1
    assert spy.calls[0]["product_kind"] == "synthesis_grounded_render"
    assert (
        spy.calls[0]["prompt_sha256"]
        == hashlib.sha256(spy.calls[0]["prompt_text"].encode()).hexdigest()
    )


@pytest.mark.asyncio
async def test_grounded_render_output_unchanged_on_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GENERATION_INPUT_RECORD", raising=False)
    baseline = await _run_render()
    monkeypatch.setattr(RunHistoryRepository, "record_generation_input", _raiser)
    again = await _run_render()
    assert again == baseline
    assert write_failures.snapshot()["generation_inputs"] >= 1


@pytest.mark.asyncio
async def test_grounded_render_flag_off_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENERATION_INPUT_RECORD", "0")
    spy = _Spy()
    spy.install(monkeypatch)
    await _run_render()
    assert spy.calls == []


@pytest.mark.asyncio
async def test_deep_dive_records_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GENERATION_INPUT_RECORD", raising=False)
    spy = _Spy()
    spy.install(monkeypatch)
    await _run_digest(monkeypatch)
    assert len(spy.calls) == 1
    assert spy.calls[0]["product_kind"] == "deep_dive"
    assert spy.calls[0]["prompt_text"] == "DIGEST PROMPT"


@pytest.mark.asyncio
async def test_deep_dive_output_unchanged_on_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GENERATION_INPUT_RECORD", raising=False)
    baseline = await _run_digest(monkeypatch)
    monkeypatch.setattr(RunHistoryRepository, "record_generation_input", _raiser)
    again = await _run_digest(monkeypatch)
    assert again == baseline
    assert write_failures.snapshot()["generation_inputs"] >= 1
