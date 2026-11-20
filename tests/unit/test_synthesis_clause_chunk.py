"""状況総括の部品を束に分けて書かせる (2026-11-20、docs/synthesis_length_gate.md §2.5)。

LLM は fake。
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from src.synthesis.grounded import render as render_mod
from src.synthesis.grounded import slot_render
from src.synthesis.grounded.estimate import Estimate
from src.synthesis.grounded.render import build_render_plan, render_sections
from src.synthesis.grounded.slot_chunk import CHUNK_ITEMS, needs_chunks, split_even
from src.synthesis.grounded.slot_render import ClauseItem, WireJudgmentClauses
from tests.unit.n20_t5_fixtures import estimate, judgment

LABEL = "2026-11-23 (weekly)"
_ID = re.compile(r"^\[(s-\d{3})\]【", re.M)


@pytest.mark.parametrize(
    ("n", "sizes"),
    [(0, []), (15, [15]), (16, [8, 8]), (33, [11, 11, 11]), (34, [12, 11, 11])],
)
def test_split_even(n: int, sizes: list[int]) -> None:
    ids = [f"s-{i:03d}" for i in range(n)]
    chunks = split_even(ids)
    assert [len(c) for c in chunks] == sizes
    assert [i for c in chunks for i in c] == ids  # 順序を保つ


def _wide(n_moved: int, n_standing: int) -> Estimate:
    moved = [judgment(f"s-{i:03d}", delta="opened") for i in range(n_moved)]
    standing = [judgment(f"s-{100 + i:03d}", delta="no_change") for i in range(n_standing)]
    return replace(estimate(), judgments=(*moved, *standing))


@pytest.fixture
def wide_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render_mod, "_MOVED_SECTION_MAX", {"daily": 40})
    monkeypatch.setattr(render_mod, "_MOVED_SECTION_MIN", {"daily": 40})
    monkeypatch.setattr(render_mod, "_STANDING_SECTION_MAX", {"daily": 20})
    monkeypatch.setattr(render_mod, "_STANDING_SECTION_MIN", 20)


class _EchoLLM:
    """プロンプトに載った判定 id を全部書き返す。呼び出しごとのプロンプトを残す。"""

    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate_structured(self, prompt: str, schema: type, **_kw: Any) -> Any:
        self.prompts.append(prompt)
        if schema is not WireJudgmentClauses:
            return render_mod._WireSections(headline="旧", weight_section="旧")
        section = prompt.split("【変化した判定】", 1)[1].split("【判定間の関係】", 1)[0]
        ids = _ID.findall(section)
        k = len(self.prompts)
        return WireJudgmentClauses(
            headline_clause=f"見出し{k}",
            items=[ClauseItem(judgment_id=i, world_clause=f"{i} の世界") for i in ids],
            watch_clause=f"注視{k}",
        )


@pytest.mark.asyncio
async def test_on_writes_in_chunks_and_merges_in_order(
    monkeypatch: pytest.MonkeyPatch, wide_caps: None
) -> None:
    monkeypatch.setenv("SLOT_RENDER", "1")
    monkeypatch.setenv("SYNTHESIS_CLAUSE_CHUNK", "1")
    est = _wide(20, 13)
    llm = _EchoLLM()

    clauses, plan = await slot_render.generate_clauses(
        llm=llm,  # type: ignore[arg-type]
        est=est,
        period_label=LABEL,
    )

    assert len(llm.prompts) == 3
    assert [i.judgment_id for i in clauses.items] == [*plan.moved_ids, *plan.standing_ids]
    assert clauses.headline_clause == "見出し1" and clauses.watch_clause == "注視1"
    assert all(len(_ID.findall(p)) <= CHUNK_ITEMS for p in llm.prompts)
    # 見出しの判定は第 1 束だけに載る
    assert "【headline 対象判定" in llm.prompts[0]
    assert all("【headline 対象判定" not in p for p in llm.prompts[1:])


@pytest.mark.asyncio
async def test_narrow_period_is_one_call_with_the_unchanged_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLOT_RENDER", "1")
    monkeypatch.setenv("SYNTHESIS_CLAUSE_CHUNK", "1")
    est = estimate()
    llm = _EchoLLM()

    _, plan = await slot_render.generate_clauses(
        llm=llm,  # type: ignore[arg-type]
        est=est,
        period_label=LABEL,
    )

    assert not needs_chunks(plan)
    assert llm.prompts == [plan.prompt]


@pytest.mark.asyncio
async def test_shadow_delivers_single_call_and_records_chunked(
    monkeypatch: pytest.MonkeyPatch, wide_caps: None, tmp_path: Path
) -> None:
    monkeypatch.setenv("SLOT_RENDER", "1")
    monkeypatch.setenv("SYNTHESIS_CLAUSE_CHUNK", "shadow")
    monkeypatch.setenv("SYNTHESIS_CHUNK_SHADOW_DIR", str(tmp_path))
    est = _wide(20, 13)
    llm = _EchoLLM()

    out = await render_sections(llm=llm, est=est, period_label=LABEL)  # type: ignore[arg-type]

    assert len(llm.prompts) == 1 + 3  # 配信 1 回 + 束 3 回
    plan = build_render_plan(est=est, period_label=LABEL, template=slot_render.TEMPLATE_V9)
    assert llm.prompts[0] == plan.prompt  # 配信は従来の 1 回の呼び出し
    assert out.spillover_section.startswith("注視1")  # 配信の部品は 1 回目の呼び出し
    rows = [json.loads(x) for f in tmp_path.glob("*.jsonl") for x in f.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["chunks"] == 3
    assert rows[0]["sections"]["weight_section"]


@pytest.mark.asyncio
async def test_shadow_failure_does_not_affect_delivery(
    monkeypatch: pytest.MonkeyPatch, wide_caps: None, tmp_path: Path
) -> None:
    monkeypatch.setenv("SLOT_RENDER", "1")
    monkeypatch.setenv("SYNTHESIS_CLAUSE_CHUNK", "shadow")
    monkeypatch.setenv("SYNTHESIS_CHUNK_SHADOW_DIR", str(tmp_path))

    class _FailSecond(_EchoLLM):
        async def generate_structured(self, prompt: str, schema: type, **kw: Any) -> Any:
            if self.prompts:
                raise RuntimeError("boom")
            return await super().generate_structured(prompt, schema, **kw)

    out = await render_sections(
        llm=_FailSecond(),  # type: ignore[arg-type]
        est=_wide(20, 13),
        period_label=LABEL,
    )

    assert out.weight_section
    assert not list(tmp_path.glob("*.jsonl"))
