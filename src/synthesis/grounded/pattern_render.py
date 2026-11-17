"""状況総括の横断パターン版の描画 (旗 SYNTHESIS_PATTERNS)。

まとめ (``patterns.build_patterns``) はコード、文はモデル (narrative ティア)、関門
(``pattern_gates``) を通らない欄は決定論の文。パターンが 1 つも立たない日は LLM を呼ばない。
旗: off = 現行 / shadow = 現行で配信し、この版を別に作って ``generation_inputs`` と件数の log に
残す (失敗は配信に影響しない) / on = この版 (失敗したら現行へ倒す)。
設計: docs/synthesis_pattern_redesign.md。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from src.logging_config import get_logger
from src.ops.generation_inputs import record_generation_input
from src.storage.repo_judgment_attrs import JudgmentAttrs
from src.synthesis.grounded.estimate import Estimate
from src.synthesis.grounded.passes import _render
from src.synthesis.grounded.pattern_fallback import fallback_headline
from src.synthesis.grounded.pattern_gates import GatedTexts, GateStats, gate_patterns
from src.synthesis.grounded.pattern_sections import assemble_pattern_sections
from src.synthesis.grounded.patterns import PatternCase, PatternPlan, build_patterns
from src.synthesis.grounded.render import (
    _DELTA_JA,
    _MAX_TOKENS,
    _MAX_TOKENS_BY_PERIOD,
    _TEMPERATURE,
    _WireSections,
)
from src.synthesis.grounded.slot_render_extras import first_sentence
from src.tools.llm_client import LLMClient
from src.tools.llm_schema import require_all_properties

_log = get_logger(__name__)

TEMPLATE_V10 = "synthesis/render_patterns.j2"
PATTERNS_MAX = 12
_CONF_JA = {"high": "高確度", "moderate": "中確度", "low": "低確度"}
_STANDING_TITLE_CHARS = 120


class PatternItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pattern_id: str = ""
    text: str = ""

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return require_all_properties(dict(handler(core_schema)))


class WirePatterns(BaseModel):
    """v10 の LLM 出力 (headline / patterns[{pattern_id, text}] / watch)。"""

    model_config = ConfigDict(extra="ignore")
    headline: str = ""
    patterns: list[PatternItem] = Field(
        default_factory=list, json_schema_extra={"maxItems": PATTERNS_MAX}
    )
    watch: str = ""

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return require_all_properties(dict(handler(core_schema)))


def _case_view(c: PatternCase) -> dict[str, object]:
    return {
        "title": c.title,
        "delta_ja": _DELTA_JA.get(c.delta_type, c.delta_type),
        "confidence_ja": _CONF_JA.get(c.confidence, c.confidence),
        "is_provider": c.is_provider,
    }


def build_prompt(plan: PatternPlan, period_label: str) -> str:
    patterns = [
        {
            "id": p.pattern_id,
            "region": p.region,
            "kind_label": p.kind_label,
            "count": p.count,
            "s3": p.s3_count,
            "provider": p.provider_count,
            "cases": [_case_view(c) for c in p.cases],
        }
        for p in plan.patterns
    ]
    standing = [
        {
            "title": first_sentence(j.claim, _STANDING_TITLE_CHARS),
            "confidence_ja": _CONF_JA.get(j.confidence, j.confidence),
        }
        for j in plan.standing
    ]
    return _render(
        TEMPLATE_V10,
        period_label=period_label,
        patterns=patterns,
        rest=[_case_view(c) for c in plan.rest],
        standing=standing,
    )


def _deterministic(plan: PatternPlan, moved_questions: Sequence[str]) -> GatedTexts:
    """パターンが無い日 (LLM を呼ばない): 見出しは静かな日の文、パターンの文は無い。"""
    return GatedTexts(headline=fallback_headline(plan, moved_questions))


async def generate_gated(
    *,
    llm: LLMClient,
    est: Estimate,
    plan: PatternPlan,
    period_label: str,
    moved_questions: Sequence[str],
) -> tuple[GatedTexts, GateStats | None]:
    if not plan.patterns:
        return _deterministic(plan, moved_questions), None
    prompt = build_prompt(plan, period_label)
    record_generation_input(
        step="synthesis_render",
        model=getattr(llm, "model", ""),
        product_kind="synthesis_pattern_render",
        product_id=f"{est.period_type}:{period_label}:patterns",
        prompt_text=prompt,
        input_data=lambda: {
            "period_label": period_label,
            "pattern_ids": [p.pattern_id for p in plan.patterns],
            "judgment_ids": [i for p in plan.patterns for c in p.cases for i in c.judgment_ids],
            "rest_ids": [i for c in plan.rest for i in c.judgment_ids],
        },
    )
    raw: WirePatterns = await llm.generate_structured(
        prompt,
        WirePatterns,
        temperature=_TEMPERATURE,
        max_tokens=_MAX_TOKENS_BY_PERIOD.get(est.period_type, _MAX_TOKENS),
        think=False,
    )
    return gate_patterns(
        headline=raw.headline,
        pattern_items=[(i.pattern_id, i.text) for i in raw.patterns],
        watch=raw.watch,
        plan=plan,
        moved_questions=moved_questions,
    )


def _log_stats(event: str, period_label: str, plan: PatternPlan, stats: GateStats | None) -> None:
    _log.info(
        event,
        period=period_label,
        patterns=len(plan.patterns),
        rest=len(plan.rest),
        moved=plan.moved_count,
        llm_called=stats is not None,
        **(stats.__dict__ if stats else {}),
        replaced_rate=round(stats.replaced_rate, 3) if stats else 0.0,
    )


async def render_pattern_sections(
    *,
    llm: LLMClient,
    est: Estimate,
    period_label: str,
    attrs: Mapping[str, JudgmentAttrs],
    moved_questions: Sequence[str] = (),
) -> _WireSections:
    """on: パターン版で節を組む。例外は呼び手 (``choose_sections``) が現行へ倒す。"""
    plan = build_patterns(est.judgments, attrs)
    gated, stats = await generate_gated(
        llm=llm, est=est, plan=plan, period_label=period_label, moved_questions=moved_questions
    )
    _log_stats("synthesis_pattern_render", period_label, plan, stats)
    return assemble_pattern_sections(est=est, plan=plan, gated=gated)


async def shadow_patterns(
    *,
    llm: LLMClient,
    est: Estimate,
    period_label: str,
    attrs: Mapping[str, JudgmentAttrs],
    moved_questions: Sequence[str] = (),
) -> None:
    """shadow: 現行で配信した後に別に作り、件数だけ記録する。失敗は配信に影響しない。"""
    try:
        plan = build_patterns(est.judgments, attrs)
        _, stats = await generate_gated(
            llm=llm, est=est, plan=plan, period_label=period_label, moved_questions=moved_questions
        )
    except Exception as e:  # noqa: BLE001 — shadow は配信に影響させない
        _log.warning("synthesis_pattern_render_shadow_failed", error=str(e)[:160])
        return
    _log_stats("synthesis_pattern_render_shadow", period_label, plan, stats)
