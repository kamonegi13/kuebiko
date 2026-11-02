"""状況総括の節をコードが組む形 (n20 T5、旗 SLOT_RENDER、利用者判断 U-B)。LLM は fake。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from src.synthesis.grounded import slot_render
from src.synthesis.grounded.render import _WireSections, build_render_plan, render_sections
from src.synthesis.grounded.slot_render import ClauseItem, WireJudgmentClauses
from tests.unit.n20_t5_fixtures import estimate, judgment

LABEL = "2026-10-20 (daily)"


def _clauses(**over: Any) -> WireJudgmentClauses:
    base: dict[str, Any] = {
        "headline_clause": "製造業を狙う侵入が拡大している",
        "items": [
            ClauseItem(judgment_id="s-aaa", world_clause="侵入が拡大した", implication_clause="i1"),
            ClauseItem(
                judgment_id="s-bbb", world_clause="新たな侵入が報告された", implication_clause="i2"
            ),
            ClauseItem(
                judgment_id="s-ccc", world_clause="侵入が続いている", implication_clause="i3"
            ),
        ],
        "watch_clause": "同種の侵入の再発を注視する",
    }
    base.update(over)
    return WireJudgmentClauses(**base)


def _plan() -> Any:
    return build_render_plan(est=estimate(), period_label=LABEL, template=slot_render.TEMPLATE_V9)


class TestCertaintySentence:
    @pytest.mark.parametrize(
        ("conf", "phrase"),
        [("high", "可能性が高い"), ("moderate", "可能性がある"), ("low", "確証はなく")],
    )
    def test_each_confidence_level(self, conf: str, phrase: str) -> None:
        j = judgment("s-x", confidence=conf)
        assert phrase in slot_render.certainty_sentence(j)
        assert "組織的国家作戦" in slot_render.certainty_sentence(j)

    @pytest.mark.parametrize("refuted", [True, False])
    @pytest.mark.parametrize("conf", ["high", "moderate", "low"])
    def test_refuted_and_counter_are_rendered_by_code(self, conf: str, refuted: bool) -> None:
        j = judgment("s-x", confidence=conf, refuted=refuted)
        text = slot_render.render_judgment(j, "世界の文", "含意の文", with_delta=True)
        assert text.startswith("【新規】世界の文。")
        assert ("反証" in text) is refuted
        assert "対立仮説として" in text  # viable な対立仮説がある
        assert text.endswith("含意の文。")

    def test_no_counter_when_no_viable_alternative(self) -> None:
        j = judgment("s-x", viable_counter=False)
        assert "対立仮説" not in slot_render.render_judgment(j, "w", "", with_delta=False)


class TestAssemble:
    def test_sections_cover_all_layers(self) -> None:
        sections, stats = slot_render.assemble_sections(_clauses(), est=estimate(), plan=_plan())
        assert isinstance(sections, _WireSections)
        assert "【拡大】侵入が拡大した。" in sections.weight_section
        assert "【新規】新たな侵入が報告された。" in sections.weight_section
        assert "侵入が続いている。" in sections.cog_section
        assert "可能性が高い" in sections.headline  # 指名判定 (high) の確度をコードが付ける
        assert "発火した監視指標: 同種の侵入の再発" in sections.spillover_section
        assert sections.chain_section == slot_render.NO_RELATIONS
        assert stats == slot_render.SlotStats()

    def test_uncovered_judgment_falls_back_to_claim(self) -> None:
        clauses = _clauses(items=[ClauseItem(judgment_id="s-aaa", world_clause="w")])
        sections, stats = slot_render.assemble_sections(clauses, est=estimate(), plan=_plan())
        assert stats.uncovered == 2
        assert "s-bbb の主張" in sections.weight_section

    def test_unknown_and_duplicate_ids(self) -> None:
        items = [
            ClauseItem(judgment_id="s-zzz", world_clause="作った id"),
            ClauseItem(judgment_id="s-aaa", world_clause="一回目"),
            ClauseItem(judgment_id="s-aaa", world_clause="二回目"),
        ]
        sections, stats = slot_render.assemble_sections(
            _clauses(items=items), est=estimate(), plan=_plan()
        )
        assert stats.unknown_ids == 1
        assert "一回目" in sections.weight_section and "二回目" not in sections.weight_section

    @pytest.mark.parametrize("word", ["可能性が高い", "とみられる", "反証", "確実に", "対立仮説"])
    def test_banned_words_in_clause_are_replaced(self, word: str) -> None:
        items = [ClauseItem(judgment_id="s-aaa", world_clause=f"侵入が拡大した{word}")]
        sections, stats = slot_render.assemble_sections(
            _clauses(items=items), est=estimate(), plan=_plan()
        )
        assert stats.banned_replaced == 1
        assert "s-aaa の主張" in sections.weight_section

    def test_causal_connector_not_in_delta_note_is_replaced(self) -> None:
        items = [ClauseItem(judgment_id="s-aaa", world_clause="報道を受けて侵入が拡大した")]
        _, stats = slot_render.assemble_sections(
            _clauses(items=items), est=estimate(), plan=_plan()
        )
        assert stats.causal_replaced == 1

    def test_listed_judgments_are_counted_not_written(self) -> None:
        plan = replace(_plan(), listed_ids=("s-bbb",))
        sections, _ = slot_render.assemble_sections(_clauses(), est=estimate(), plan=plan)
        assert "そのほかの動き (根拠は本文で扱わない判定): 新規 1 件。" in sections.weight_section

    def test_output_shape_passes_the_n20_training_gate(self) -> None:
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "sft_gate_n20_schemas", Path("scripts/sft_gate_n20_schemas.py")
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = json.loads(_clauses().model_dump_json())
        assert mod.schema_errors_synthesis(out, _plan().prompt) == []

    def test_v9_prompt_lists_judgment_ids(self) -> None:
        prompt = _plan().prompt
        assert "[s-aaa]" in prompt and "headline_clause" in prompt
        assert "weight_section" not in prompt  # 旧の 6 節は書かせない


class _LLM:
    model = "fake"

    def __init__(self) -> None:
        self.schemas: list[type] = []

    async def generate_structured(self, prompt: str, schema: type, **_kw: Any) -> Any:
        self.schemas.append(schema)
        if schema is WireJudgmentClauses:
            return _clauses()
        return _WireSections(headline="旧の見出し" * 20, weight_section="旧")


@pytest.mark.asyncio
async def test_on_mode_renders_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLOT_RENDER", "1")
    llm = _LLM()
    out = await render_sections(llm=llm, est=estimate(), period_label=LABEL)  # type: ignore[arg-type]
    assert llm.schemas == [WireJudgmentClauses]
    assert "【拡大】" in out.weight_section


@pytest.mark.asyncio
async def test_shadow_mode_keeps_legacy_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLOT_RENDER", "shadow")
    seen: list[str] = []
    monkeypatch.setattr(slot_render._log, "info", lambda e, **kw: seen.append(e))
    llm = _LLM()
    out = await render_sections(llm=llm, est=estimate(), period_label=LABEL)  # type: ignore[arg-type]
    assert llm.schemas == [_WireSections, WireJudgmentClauses]
    assert out.weight_section == "旧"
    assert "synthesis_slot_render_shadow" in seen
