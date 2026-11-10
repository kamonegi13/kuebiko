"""生成物の入力の記録 (P0-18) のテスト。"""

from __future__ import annotations

import contextlib
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
    data = json.loads(spy.calls[0]["input_json"])
    assert data["estimate"]["period_type"]
    assert data["period_label"] == "L"
    assert data["ranked_ids"]
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
    data = json.loads(spy.calls[0]["input_json"])
    assert data["selected_ids"] == ["a1"]
    assert data["candidates"][0]["summary"] == "要約"


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


# --- 残りの生成サイト (Spotlight / 旧 synthesis / PIR 日次要点 / 事象ニュース) ----------------


def _use_spy_or_raiser(monkeypatch: pytest.MonkeyPatch, *, fail: bool) -> _Spy:
    monkeypatch.delenv("GENERATION_INPUT_RECORD", raising=False)
    spy = _Spy()
    if fail:
        monkeypatch.setattr(RunHistoryRepository, "record_generation_input", _raiser)
    else:
        spy.install(monkeypatch)
    return spy


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.asyncio
async def test_pir_daily_focus_records_prompt_and_survives_failure(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    from unittest.mock import AsyncMock

    from src.digest import pir_daily_focus as mod
    from src.pir.models import Pir, SpotlightConfig, StrongSignals

    spy = _use_spy_or_raiser(monkeypatch, fail=fail)
    pir = Pir(
        id="pir_test",
        title="テスト",
        description="d",
        enabled=True,
        strong_signals=StrongSignals(keywords=["t"]),
        spotlight=SpotlightConfig(enabled=False),
    )
    llm = AsyncMock()
    llm.model = "m"
    llm.generate = AsyncMock(return_value=type("R", (), {"text": "既知の手口が続いた。"})())
    out = await mod._generate_llm_summary(llm, pir, [])
    assert out == "既知の手口が続いた。"
    sent = llm.generate.await_args.kwargs["prompt"]
    if fail:
        assert write_failures.snapshot()["generation_inputs"] >= 1
    else:
        assert [c["prompt_text"] for c in spy.calls] == [sent]
        assert spy.calls[0]["product_kind"] == "pir_daily_focus"
        data = json.loads(spy.calls[0]["input_json"])
        assert data["pir_id"] == "pir_test"
        assert data["matches"] == []


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.asyncio
async def test_legacy_synthesis_records_prompt_and_survives_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: bool
) -> None:
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from src.storage.run_history import ArticleRecord, RunRecord
    from src.synthesis import generator as syn
    from src.synthesis.generator import generate_synthesis
    from src.tools.llm_client import LLMClient, LLMResponse

    monkeypatch.setenv("SYNTHESIS_GROUNDED", "0")
    spy = _use_spy_or_raiser(monkeypatch, fail=fail)
    # 入力データの組み立ては本テストの関心外 (既定 DB を読むため固定する)
    monkeypatch.setattr(syn, "_build_axes_data", lambda **k: [{"total_current": 3}])
    monkeypatch.setattr(syn, "_build_trend_clusters_data", lambda **k: [])
    monkeypatch.setattr(syn, "_build_high_importance_cross_axis", lambda **k: [])
    monkeypatch.setattr(syn, "_load_previous_synthesis", lambda **k: None)
    monkeypatch.setattr(syn, "_render_prompt", lambda *a, **k: "SYN PROMPT")
    repo = RunHistoryRepository(db_path=tmp_path / "s.db")
    run_id = repo.start_run(
        RunRecord(started_at=datetime(2026, 10, 31, tzinfo=UTC), pipeline="x", dry_run=False)
    )
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id="a",
            title="Sample",
            url="https://kuebiko.example/a",
            status="posted",
            pmesii_m=True,
            pmesii_i_cyber=True,
        )
    )
    llm = AsyncMock(spec=LLMClient)
    llm.model = "m"
    llm.generate = AsyncMock(return_value=LLMResponse(text='{"headline": "x"}', model="m"))
    result = await generate_synthesis(llm=llm, period_type="daily", db_path=repo._db_path)
    assert result.record is None  # 必須欄不足 = 出力は記録の有無に依らず同じ
    assert result.error is not None
    assert "no articles" not in result.error
    # 必須欄不足で 1 回だけ書き直す = 送った prompt 2 本が両方記録される
    assert llm.generate.await_count == 2
    sent = [c.kwargs["prompt"] for c in llm.generate.await_args_list]
    if fail:
        assert write_failures.snapshot()["generation_inputs"] >= 2
    else:
        assert [c["prompt_text"] for c in spy.calls] == sent
        assert spy.calls[0]["product_kind"] == "synthesis"
        assert spy.calls[1]["product_id"].endswith(":retry1")
        first = json.loads(spy.calls[0]["input_json"])
        retry = json.loads(spy.calls[1]["input_json"])
        assert first["period_type"] == "daily"
        assert first["axes_data"] == [{"total_current": 3}]
        assert retry["retry_missing"]
        assert {k: v for k, v in retry.items() if k != "retry_missing"} == first


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.asyncio
async def test_spotlight_records_prompt_and_survives_failure(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from src.spotlight import generator as gen
    from src.spotlight.generator import generate_spotlight
    from tests.unit.test_spotlight_identifier_gate import _FakeLLM, _FakeMatch, _FakePir

    spy = _use_spy_or_raiser(monkeypatch, fail=fail)
    matches = [_FakeMatch(article_id=f"a{i}") for i in range(6)]
    monkeypatch.setattr(gen, "evaluate_pir_matches", lambda *a, **k: matches)
    monkeypatch.setattr(gen, "_build_prompt", lambda *a, **k: "SPOT PROMPT")
    monkeypatch.setattr(gen, "_load_previous_spotlight", lambda **k: None)
    monkeypatch.setattr(gen, "_build_ledger_context", lambda *a, **k: [])

    class _Repo:
        def entities_for_articles(self, *a: Any, **k: Any) -> dict[str, list[str]]:
            return {}

    monkeypatch.setattr(gen, "RunHistoryRepository", _Repo)
    monkeypatch.setenv("SPOTLIGHT_TAIL_GATE", "0")
    monkeypatch.setenv("SPOTLIGHT_IDENTIFIER_GATE", "0")
    llm = _FakeLLM()
    record = await generate_spotlight(
        _FakePir(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        assessment=SimpleNamespace(freshness={"lag_hours": 3}),  # type: ignore[arg-type]
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert record is not None
    if fail:
        assert write_failures.snapshot()["generation_inputs"] >= 1
    else:
        assert [c["prompt_text"] for c in spy.calls] == [llm.prompts[0]]
        assert spy.calls[0]["product_kind"] == "spotlight"
        data = json.loads(spy.calls[0]["input_json"])
        assert data["period_type"] == "weekly"
        assert [c["article_id"] for c in data["candidates"]] == [f"a{i}" for i in range(6)]
        assert data["graph_context"] == ""
        assert data["freshness"] == {"lag_hours": 3}


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.asyncio
async def test_eventnews_records_base_prompt_and_survives_failure(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from src.eventnews import generator as gen
    from src.eventnews import identifier_gate, quantities
    from src.eventnews import runner as rn
    from src.eventnews.models import EventNewsDraft, GateResult

    spy = _use_spy_or_raiser(monkeypatch, fail=fail)
    member = SimpleNamespace(
        article_id="m1",
        title="題",
        url="https://kuebiko.example/m1",
        feed_title="F",
        importance="high",
        category="apt",
        kind="",
        account_class="",
        anchor_ts=datetime(2026, 10, 30, tzinfo=UTC),
        summary="要約",
        body="本文",
    )
    draft = EventNewsDraft(headline="見出し", bluf="要旨")
    gate = GateResult(
        draft=draft, dropped_lines=0, repaired_ids=0, substituted_ids=0, verified=True
    )

    async def fake_draft(*a: Any, **k: Any) -> EventNewsDraft:
        return draft

    monkeypatch.setattr(gen, "select_members", lambda m: (list(m), []))
    monkeypatch.setattr(gen, "build_prompt", lambda *a, **k: "BASE PROMPT")
    monkeypatch.setattr(gen, "generate_draft", fake_draft)
    monkeypatch.setattr(rn, "_should_generate", lambda *a, **k: True)
    monkeypatch.setattr(rn, "_allowed_identifiers_text", lambda *a, **k: "")
    monkeypatch.setattr(rn, "_rewrite_hints", lambda *a, **k: [])
    monkeypatch.setattr(identifier_gate, "verify_draft", lambda *a, **k: gate)
    monkeypatch.setattr(identifier_gate, "build_member_catalog", lambda *a, **k: {})
    monkeypatch.setattr(quantities, "supporting_texts", lambda *a, **k: [])

    stored: list[dict[str, Any]] = []

    class _EventRepo:
        def atomic(self) -> Any:  # PG-ATOMIC-FIX (runner が版の記録を 1 トランザクションにした) に合わせる
            return contextlib.nullcontext()

        def record_event_version(self, **kwargs: Any) -> None:
            stored.append(kwargs)

        def record_generation_input(self, **kwargs: Any) -> None:
            if fail:
                raise RuntimeError("db down")
            spy.calls.append(kwargs)

    item = SimpleNamespace(
        members=[member],
        snapshot=SimpleNamespace(
            item_id="ev1",
            current_version=0,
            importance="high",
            status="new",
            first_reported_at=datetime(2026, 10, 30, tzinfo=UTC),
            last_reported_at=datetime(2026, 10, 31, tzinfo=UTC),
            member_ids=(member.article_id,),
        ),
    )
    llm = SimpleNamespace(model="m")
    out_gate, _ = await rn._generate_version(
        _EventRepo(),  # type: ignore[arg-type]
        item,  # type: ignore[arg-type]
        llm,  # type: ignore[arg-type]
        datetime(2026, 10, 31, tzinfo=UTC),
        "[]",
    )
    assert out_gate is gate  # 出力は記録の成否に依らない
    assert stored[0]["prompt_text"] == "BASE PROMPT"
    if fail:
        assert write_failures.snapshot()["generation_inputs"] >= 1
    else:
        assert [c["prompt_text"] for c in spy.calls] == ["BASE PROMPT"]
        assert spy.calls[0]["product_id"] == "ev1:v1"
        data = json.loads(spy.calls[0]["input_json"])
        assert data["item_id"] == "ev1"
        assert data["version"] == 1
        assert [m["article_id"] for m in data["members"]] == [member.article_id]
        assert data["omitted_ids"] == []
        assert "body" not in data["members"][0]


def test_counts_return_only_kind_counts_not_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GENERATION_INPUT_RECORD", raising=False)
    repo = RunHistoryRepository(db_path=tmp_path / "c.db")
    _record(repo)
    _record(repo)
    counts = repo.generation_inputs_counts(days=1)
    assert counts == {"eventnews": 2}
    assert PROMPT not in json.dumps(counts, ensure_ascii=False)


def test_default_repo_is_reused() -> None:
    generation_inputs.reset_for_tests()
    assert generation_inputs._default_repo() is generation_inputs._default_repo()
    generation_inputs.reset_for_tests()


def test_conftest_disables_recording_by_default() -> None:
    import os

    assert os.environ.get("GENERATION_INPUT_RECORD") == "0"


@pytest.mark.asyncio
async def test_deep_dive_retry_prompt_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.digest import llm_digest as mod
    from src.digest.db_filter import DigestCandidate
    from src.digest.recap_render import RecapOutput, RecapSection

    monkeypatch.delenv("GENERATION_INPUT_RECORD", raising=False)
    spy = _Spy()
    spy.install(monkeypatch)
    sent: list[str] = []

    async def fake(llm: Any, prompt: str, *, max_tokens: int, think: Any) -> RecapOutput:
        sent.append(prompt)
        ids = ["a1"] if len(sent) == 1 else ["a1", "a2"]
        return RecapOutput(sections=[RecapSection(heading="節", body="あ" * 600, article_ids=ids)])

    monkeypatch.setattr(mod, "_generate_sections", fake)
    monkeypatch.setattr(mod, "_render_prompt", lambda *a, **k: "DIGEST PROMPT")

    def cand(i: str) -> DigestCandidate:
        return DigestCandidate(
            article_id=i,
            title="題",
            url=f"https://kuebiko.example/{i}",
            feed_title="F",
            summary="要約",
            importance="high",
            category="apt",
            posted_channel="watch",
            dedup_key=None,
            created_at="2026-09-20T00:00:00Z",
            discord_channel_id=None,
            discord_message_id=None,
        )

    await mod.generate_digest(
        llm=object(),  # type: ignore[arg-type]
        candidates=[cand("a1"), cand("a2")],
        template_name="digest/weekly_recap.j2",
        period_label="P",
    )
    assert len(sent) == 2
    assert [c["prompt_text"] for c in spy.calls] == sent
    assert spy.calls[1]["product_id"].endswith(":retry1")
    retry = json.loads(spy.calls[1]["input_json"])
    assert retry["retry_uncited_ids"] == ["a2"]
    assert retry["retry_previous_output"]["sections"]
    assert "retry_uncited_ids" not in json.loads(spy.calls[0]["input_json"])


# --- input_json (U-E): 直列化・上限・保存・移行 ---------------------------------------------


def test_serialize_input_handles_dataclass_datetime_set() -> None:
    import dataclasses
    from datetime import UTC, datetime

    @dataclasses.dataclass(frozen=True)
    class Row:
        n: int

    text = generation_inputs.serialize_input(
        {
            "when": datetime(2026, 11, 2, tzinfo=UTC),
            "rows": (Row(1),),
            "ids": frozenset({"b", "a"}),
            "ja": "日本語",
        }
    )
    assert json.loads(text) == {
        "when": "2026-11-02T00:00:00+00:00",
        "rows": [{"n": 1}],
        "ids": ["a", "b"],
        "ja": "日本語",
    }
    assert "日本語" in text  # ensure_ascii=False


def test_serialize_input_is_bounded_by_dropping_largest_fields() -> None:
    data = {"small": [1, 2], "big": "x" * 5000, "mid": "y" * 500}
    text = generation_inputs.serialize_input(data, limit=1000)
    out = json.loads(text)
    assert len(text) <= 1000
    assert out["_truncated"] is True
    assert out["big"] == {
        "_dropped_chars": len(json.dumps({"big": "x" * 5000}, separators=(",", ":")))
    }
    assert out["small"] == [1, 2]
    assert out["mid"] == "y" * 500


def test_serialize_input_small_payload_has_no_truncated_mark() -> None:
    assert "_truncated" not in generation_inputs.serialize_input({"a": 1})


def test_input_json_is_stored_and_null_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GENERATION_INPUT_RECORD", raising=False)
    repo = RunHistoryRepository(db_path=tmp_path / "g.db")
    generation_inputs.record_generation_input(
        step="s",
        model="m",
        product_kind="k",
        product_id="1",
        prompt_text=PROMPT,
        repo=repo,
        input_data=lambda: {"ids": ["a1", "a2"]},
    )
    _record(repo)
    rows = _rows(repo)
    assert json.loads(rows[0]["input_json"]) == {"ids": ["a1", "a2"]}
    assert rows[1]["input_json"] is None


def test_input_data_failure_keeps_prompt_row_and_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GENERATION_INPUT_RECORD", raising=False)
    repo = RunHistoryRepository(db_path=tmp_path / "g.db")

    def boom() -> dict[str, Any]:
        raise KeyError("x")

    generation_inputs.record_generation_input(
        step="s",
        model="m",
        product_kind="k",
        product_id="1",
        prompt_text=PROMPT,
        repo=repo,
        input_data=boom,
    )
    rows = _rows(repo)
    assert len(rows) == 1 and rows[0]["input_json"] is None
    assert write_failures.snapshot()["generation_inputs"] == 1


def test_input_data_not_built_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENERATION_INPUT_RECORD", "0")
    called: list[int] = []

    def build() -> dict[str, Any]:
        called.append(1)
        return {}

    generation_inputs.record_generation_input(
        step="s",
        model="m",
        product_kind="k",
        product_id="1",
        prompt_text=PROMPT,
        input_data=build,
    )
    assert called == []


def test_sqlite_migration_adds_input_json_idempotently(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE generation_inputs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " recorded_at TEXT NOT NULL, step TEXT NOT NULL, model TEXT NOT NULL,"
        " product_kind TEXT NOT NULL, product_id TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,"
        " prompt_chars INTEGER NOT NULL, prompt_text TEXT NOT NULL)"
    )
    con.commit()
    con.close()
    RunHistoryRepository(db_path=db)
    RunHistoryRepository(db_path=db)  # 2 回目も落ちない
    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(generation_inputs)")]
    con.close()
    assert cols.count("input_json") == 1


def test_pg_schema_has_idempotent_input_json_migration() -> None:
    from src.storage import pg_schema, schema_sql

    assert (
        "ALTER TABLE generation_inputs ADD COLUMN IF NOT EXISTS input_json TEXT"
        in pg_schema.PG_SCHEMA_SQL
    )
    assert "input_json" in schema_sql._SCHEMA_SQL


def test_insert_translates_for_pg() -> None:
    from src.storage.db_backend import translate_sql

    sql = (
        "INSERT INTO generation_inputs (recorded_at, step, model, product_kind,"
        " product_id, prompt_sha256, prompt_chars, prompt_text, input_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    out = translate_sql(sql)
    assert out.count("%s") == 9 and "?" not in out


def test_public_exports_forbid_input_json() -> None:
    assert "input_json" in export_public_site._FORBIDDEN_KEYS


def test_serialize_input_dumps_pydantic_model_as_dict() -> None:
    from pydantic import BaseModel

    class M(BaseModel):
        a: int
        b: list[str]

    out = json.loads(generation_inputs.serialize_input({"m": M(a=1, b=["x"])}))
    assert out["m"] == {"a": 1, "b": ["x"]}


def test_serialize_input_truncation_is_within_limit_and_fast() -> None:
    import time

    data = {f"f{i}": "あ" * 20_000 for i in range(40)}  # 約 80 万字
    start = time.perf_counter()
    text = generation_inputs.serialize_input(data)
    elapsed = time.perf_counter() - start
    assert len(text) <= generation_inputs.MAX_INPUT_JSON_CHARS
    assert json.loads(text)["_truncated"] is True
    assert elapsed < 1.0  # 実測は数十 ms。同期呼び出しで許容できる目安


def test_identifier_truncation_logs_without_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GENERATION_INPUT_RECORD", "1")
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        generation_inputs._log, "warning", lambda event, **kw: seen.append({"event": event, **kw})
    )
    generation_inputs.record_generation_input(
        step="s", model="m", product_kind="k", product_id="secret" * 100, prompt_text="p", repo=repo
    )
    assert seen and seen[0]["event"] == "generation_input_field_truncated"
    assert seen[0]["field"] == "product_id"
    assert "secret" not in repr(seen[0])
