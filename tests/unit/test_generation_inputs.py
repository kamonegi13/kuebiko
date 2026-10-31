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
