"""状況総括の横断パターン版の描画 (旗 SYNTHESIS_PATTERNS)。

まとめ (``patterns.build_patterns``) はコード、文はモデル (narrative ティア)、関門
(``pattern_gates``) を通らない欄は決定論の文。パターンが 1 つも立たない日は LLM を呼ばない。
旗: off = 現行 / shadow = 現行で配信し、この版を別に作って ``generation_inputs`` と件数の log に
残す (失敗は配信に影響しない) / on = この版 (失敗したら現行へ倒す)。
設計: docs/synthesis_pattern_redesign.md。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from src.cti.attack_techniques import load_technique_catalog
from src.logging_config import get_logger
from src.ops.generation_inputs import record_generation_input
from src.storage.repo_judgment_attrs import JudgmentAttrs
from src.synthesis.grounded.estimate import Estimate
from src.synthesis.grounded.passes import _render
from src.synthesis.grounded.pattern_case import PatternCase, first_full_sentence
from src.synthesis.grounded.pattern_fallback import fallback_headline
from src.synthesis.grounded.pattern_gates import GatedTexts, GateStats, gate_patterns
from src.synthesis.grounded.pattern_sections import assemble_pattern_sections
from src.synthesis.grounded.patterns import STANDING_MAX, PatternPlan, build_patterns
from src.synthesis.grounded.render import (
    _DELTA_JA,
    _MAX_TOKENS,
    _MAX_TOKENS_BY_PERIOD,
    _TEMPERATURE,
    _WireSections,
)
from src.tools.llm_client import LLMClient
from src.tools.llm_schema import require_all_properties

_log = get_logger(__name__)

TEMPLATE_V10 = "synthesis/render_patterns.j2"
PATTERNS_MAX = 12
_CONF_JA = {"high": "高確度", "moderate": "中確度", "low": "低確度"}


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
    }


def build_prompt(plan: PatternPlan, period_label: str) -> str:
    patterns = [
        {
            "id": p.pattern_id,
            "region": p.region,
            "kind_label": p.kind_label,
            "count": p.count,
            "s3": p.s3_count,
            "cases": [_case_view(c) for c in p.cases],
            "marks": [m.label for m in p.common_marks],
        }
        for p in plan.patterns
    ]
    standing = [
        {"title": i.title, "confidence_ja": _CONF_JA.get(i.confidence, i.confidence)}
        for i in plan.standing_items[:STANDING_MAX]
    ]
    return _render(
        TEMPLATE_V10,
        period_label=period_label,
        patterns=patterns,
        notable=[
            {**_case_view(n.case), "id": n.item_id, "claim": first_full_sentence(n.case.claims[0])}
            for n in plan.notable
        ],
        rest=[_case_view(c) for c in plan.rest],
        standing=standing,
        subjects=[{"title": c.title} for c in plan.headline_subjects],
    )


@lru_cache(maxsize=1)
def _technique_names() -> Mapping[str, str]:
    """ATT&CK の技術 ID → 英語名 (辞書が無ければ空 = ID だけで表示)。日本語名の SSoT は無い。"""
    return {tid: info.name for tid, info in load_technique_catalog().items()}


def make_plan(est: Estimate, attrs: Mapping[str, JudgmentAttrs]) -> PatternPlan:
    return build_patterns(est.judgments, attrs, _technique_names())


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
    if not plan.patterns and not plan.notable:
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
            "notable_ids": [i for n in plan.notable for i in n.case.judgment_ids],
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
        notable=len(plan.notable),
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
    plan = make_plan(est, attrs)
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
    """shadow: 現行で配信した後に別に作り、節の全文を JSONL に残す。失敗は配信に影響しない。

    新旧の比較 (5 回目の判定) には文が要るため、件数の log に加えて組んだ節を
    ``pattern_shadow_store`` がローカルの data/ に追記する (DB・公開面には出さない)。
    """
    from src.synthesis.grounded.pattern_shadow_store import append_shadow_row

    try:
        plan = make_plan(est, attrs)
        gated, stats = await generate_gated(
            llm=llm, est=est, plan=plan, period_label=period_label, moved_questions=moved_questions
        )
        sections = assemble_pattern_sections(est=est, plan=plan, gated=gated)
    except Exception as e:  # noqa: BLE001 — shadow は配信に影響させない
        _log.warning("synthesis_pattern_render_shadow_failed", error=str(e)[:160])
        return
    _log_stats("synthesis_pattern_render_shadow", period_label, plan, stats)
    append_shadow_row(
        est=est,
        period_label=period_label,
        model=getattr(llm, "model", ""),
        plan=plan,
        stats=stats,
        sections=sections,
    )
