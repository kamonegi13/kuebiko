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


@pytest.fixture(autouse=True)
def public_persona_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """運用者の私的な persona の有無に依らず公開版 _persona.j2 で描画する。"""
    import jinja2

    original = jinja2.FileSystemLoader.get_source

    def get_source(self: jinja2.FileSystemLoader, environment: Any, template: str) -> Any:
        if template.endswith("_persona_local.j2"):
            raise jinja2.TemplateNotFound(template)
        return original(self, environment, template)

    monkeypatch.setattr(jinja2.FileSystemLoader, "get_source", get_source)


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


@pytest.mark.asyncio
async def test_spotlight_off_calls_single_stage_once(
    flags_off: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit import test_spotlight_staged as t

    t._patch(monkeypatch)
    llm = t._StagedLLM()
    record = await t._run(llm)
    assert llm.schemas == ["_LLMSpotlightOutput"]
    assert record.outlook == "旧の見通し"


def test_dedup_items_matches_legacy_implementation() -> None:
    from collections.abc import Sequence

    from src.eventnews.list_dedup import _is_near_dup, _key, dedup_items
    from src.eventnews.models import FactItem

    def legacy[T: (str, FactItem)](items: Sequence[T]) -> list[T]:
        kept_keys: list[str] = []
        kept: list[T] = []
        for item in items:
            key = _key(item)
            if key in kept_keys or _is_near_dup(key, kept_keys):
                continue
            kept_keys.append(key)
            kept.append(item)
        return kept

    long_a = "The vendor confirmed that the intrusion affected the VPN appliance in the field."
    long_b = "The vendor confirmed that the intrusion affected the VPN appliances in the field."
    strs = ["a  b", "a b", "c", long_a, long_b, "被害は 80 組織", "被害は 80 組織超"]
    assert dedup_items(strs) == legacy(strs)
    facts = [FactItem(text=t, source_index=1) for t in strs]
    assert dedup_items(facts) == legacy(facts)
