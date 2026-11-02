"""n20 T5 の旗が既定 (off) のとき、本番のプロンプト・呼び出し・描画が変わらないことの固定。

ハッシュは旗を足す前の main (8e8bc479) のコードで同じ固定入力から計算した値。
旗を足した後も一致しなければ、既定の経路が変わっている (= 出力が変わる)。LLM は呼ばない。
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from tests.unit.n20_t5_fixtures import ALLOWED, estimate, members

#: main 8e8bc479 の build_prompt (eventnews/refine.j2) の sha256
GOLDEN_EVENTNEWS_PROMPT = "d18606d85ff366f61ca528864a5e2a1009fa6c4527cdb09018796ca54d3e7d2e"
#: main 8e8bc479 の build_render_plan (synthesis/render.j2) の sha256
GOLDEN_RENDER_PROMPT = "d03ffdf4fc4e5dc232a2d4241dd62835f2442a5ce13f60d02aa94d3df04014d8"

_FLAGS = ("FACT_ANCHOR", "SLOT_RENDER", "SPOTLIGHT_STAGED", "DEEP_DIVE_SLOT_RENDER")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(params=["unset", "0", "off", "bogus"])
def flags_off(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    for env in _FLAGS:
        if request.param == "unset":
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, request.param)
    return str(request.param)


def test_flag_modes_are_off(flags_off: str) -> None:
    from src.tools import n20_flags

    assert n20_flags.fact_anchor_mode() == "off"
    assert n20_flags.slot_render_mode() == "off"
    assert n20_flags.spotlight_staged_mode() == "off"
    assert n20_flags.deep_dive_slot_render_mode() == "off"


def test_eventnews_prompt_is_byte_identical(flags_off: str) -> None:
    from src.eventnews.generator import build_prompt

    assert _sha(build_prompt(members(), ALLOWED)) == GOLDEN_EVENTNEWS_PROMPT


def test_render_prompt_is_byte_identical(flags_off: str) -> None:
    from src.synthesis.grounded.render import build_render_plan

    plan = build_render_plan(est=estimate(), period_label="2026-10-20 (daily)")
    assert _sha(plan.prompt) == GOLDEN_RENDER_PROMPT


class _RecordingLLM:
    model = "fake"

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls: list[tuple[str, type]] = []

    async def generate_structured(self, prompt: str, schema: type, **_kw: Any) -> Any:
        self.calls.append((prompt, schema))
        return self.reply(schema)


@pytest.mark.asyncio
async def test_eventnews_generate_calls_v6_schema_once(flags_off: str) -> None:
    from src.eventnews.generator import build_prompt, generate_draft
    from src.eventnews.models import EventNewsDraft, FactItem

    def reply(schema: type) -> Any:
        return EventNewsDraft(
            headline="h",
            bluf="b",
            facts=[FactItem(text="t", source_index=1)],
            caveats=[FactItem(text="c", source_index=1)],
            unknowns=["u"],
        )

    llm = _RecordingLLM(reply)
    draft = await generate_draft(members(), ALLOWED, llm)  # type: ignore[arg-type]
    assert [s for _, s in llm.calls] == [EventNewsDraft]
    assert llm.calls[0][0] == build_prompt(members(), ALLOWED)
    assert type(draft) is EventNewsDraft


@pytest.mark.asyncio
async def test_render_sections_uses_legacy_schema_once(flags_off: str) -> None:
    from src.synthesis.grounded.render import _WireSections, build_render_plan, render_sections

    def reply(schema: type) -> Any:
        return _WireSections(
            headline="見出し" * 30,
            weight_section="w",
            chain_section="c",
            cog_section="g",
            spillover_section="s",
            pir_section="p",
        )

    llm = _RecordingLLM(reply)
    out = await render_sections(llm=llm, est=estimate(), period_label="2026-10-20 (daily)")  # type: ignore[arg-type]
    assert [s for _, s in llm.calls] == [_WireSections]
    plan = build_render_plan(est=estimate(), period_label="2026-10-20 (daily)")
    assert llm.calls[0][0] == plan.prompt
    assert out.weight_section == "w" and out.pir_section == "p"
