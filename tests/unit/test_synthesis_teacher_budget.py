"""状況総括の教師収穫における長さ制御 (収穫時に落とす) の不変量。

MLX 学習の系列長メモリ壁は ~14.1k トークン。学習に使えない長さの対を外部枠で作っても
捨てるだけなので、**予算超過の窓は教師 (外部) に回さない**。2026-09-15 実測:
凍結 15 窓の render プロンプトは中央 11.0k / 最大 13.9k トークン。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from build_sft_teacher_synthesis import (  # noqa: E402
    _RENDER_MARKER,
    SelectiveTeacherClient,
    _est_tokens,
    _has_notes,
)


class _Sections(BaseModel):
    analysis_notes: str = ""
    headline: str = ""


class _Arm:
    def __init__(self, name: str) -> None:
        self.model = name
        self.calls = 0

    async def generate_structured(self, prompt: str, schema: type, **kw: Any) -> Any:
        self.calls += 1
        return schema(headline=f"{self.model} の出力")


def _client(max_prompt_tokens: int) -> tuple[SelectiveTeacherClient, _Arm, _Arm]:
    teacher, local = _Arm("teacher"), _Arm("local")
    client = SelectiveTeacherClient(teacher, local, max_prompt_tokens=max_prompt_tokens)
    return client, teacher, local


class TestTokenEstimate:
    def test_estimate_uses_measured_chars_per_token(self) -> None:
        # 実測比 1.85 (1.76-1.91 の中央付近) — 18,500 字 ≒ 10k トークン
        assert 9_500 <= _est_tokens("あ" * 18_500) <= 10_500


class TestOversizeRouting:
    @pytest.mark.asyncio
    async def test_oversized_render_never_reaches_the_teacher(self) -> None:
        client, teacher, local = _client(max_prompt_tokens=100)
        await client.generate_structured(_RENDER_MARKER + "x" * 10_000, _Sections)
        assert teacher.calls == 0
        assert local.calls == 1
        assert client.oversize == 1
        assert client.captured == []  # 捕獲もしない (ローカル出力の教師混入防止)

    @pytest.mark.asyncio
    async def test_render_within_budget_goes_to_the_teacher_and_is_captured(self) -> None:
        client, teacher, local = _client(max_prompt_tokens=10_000)
        await client.generate_structured(_RENDER_MARKER + "短い", _Sections)
        assert teacher.calls == 1
        assert local.calls == 0
        assert client.oversize == 0
        assert len(client.captured) == 1

    @pytest.mark.asyncio
    async def test_non_render_calls_always_go_local(self) -> None:
        client, teacher, local = _client(max_prompt_tokens=10_000)
        await client.generate_structured("ACH の採点プロンプト", _Sections)
        assert teacher.calls == 0
        assert local.calls == 1
        assert client.captured == []


class TestNotesGate:
    def test_empty_or_missing_notes_is_rejected(self) -> None:
        assert not _has_notes(json.dumps({"headline": "h"}))
        assert not _has_notes(json.dumps({"analysis_notes": "  ", "headline": "h"}))
        assert not _has_notes("JSON ではない")

    def test_filled_notes_is_accepted(self) -> None:
        assert _has_notes(json.dumps({"analysis_notes": "確度の対応付け…", "headline": "h"}))
