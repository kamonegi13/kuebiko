"""パターン版の shadow が節の全文を JSONL に残すこと (旗 SYNTHESIS_PATTERNS=shadow)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.synthesis.grounded import pattern_shadow_store
from src.synthesis.grounded.pattern_shadow_store import SHADOW_DIR_ENV
from tests.unit.test_synthesis_pattern_render import _LLM, _render


@pytest.fixture
def shadow_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(SHADOW_DIR_ENV, str(tmp_path / "shadow"))
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "shadow")
    return tmp_path / "shadow"


def _rows(d: Path) -> list[dict[str, object]]:
    files = sorted(d.glob("*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text("utf-8").splitlines()]


@pytest.mark.asyncio
async def test_shadow_appends_full_sections_and_delivers_current(shadow_dir: Path) -> None:
    out = await _render(_LLM())
    assert out.weight_section == "旧"
    rows = _rows(shadow_dir)
    assert len(rows) == 1
    row = rows[0]
    assert row["period_type"] == "daily"
    assert row["period_label"] == "2026-11-17 (daily)"
    assert row["patterns"] == 1
    sections = row["sections"]
    assert isinstance(sections, dict)
    assert "アルファ証券" in str(sections["headline"])


@pytest.mark.asyncio
async def test_two_runs_append_two_lines(shadow_dir: Path) -> None:
    await _render(_LLM())
    await _render(_LLM())
    assert len(_rows(shadow_dir)) == 2


@pytest.mark.asyncio
async def test_off_writes_nothing(shadow_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "0")
    await _render(_LLM())
    assert not shadow_dir.exists()


@pytest.mark.asyncio
async def test_llm_failure_writes_nothing(shadow_dir: Path) -> None:
    out = await _render(_LLM(fail=True))
    assert out.weight_section == "旧"
    assert not shadow_dir.exists()


@pytest.mark.asyncio
async def test_write_failure_never_affects_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(SHADOW_DIR_ENV, str(blocker / "sub"))  # 親がファイル = mkdir が失敗
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "shadow")
    seen: list[str] = []
    monkeypatch.setattr(pattern_shadow_store._log, "warning", lambda e, **_kw: seen.append(e))
    out = await _render(_LLM())
    assert out.weight_section == "旧"
    assert seen == ["synthesis_pattern_shadow_write_failed"]
