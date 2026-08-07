"""段3: 敵対的検証 (対称 red-team)。

独立パスが各 judgment の leading を **方向に依らず** 反証し、最有力の対立論拠を立てる。
反証が成立すれば確度を low に降格し (必要なら leading を別仮説に flip)、対立論拠を記録する。
穏当側にも脅威側にも誘導しない (docs/synthesis_reliability_redesign.md 原則0)。
適用 (apply_adversarial) は純関数で決定論的。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from pydantic import BaseModel, Field

from src.logging_config import get_logger
from src.synthesis.grounded.estimate import (
    KeyJudgment,
    is_counts_refuted,
    reconcile_hypotheses,
)
from src.synthesis.grounded.passes import _render  # prompt render 共有
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

_TEMPERATURE = 0.2
_MAX_TOKENS = 4_000


class _WireReview(BaseModel):
    model_config = {"extra": "ignore"}
    judgment_id: str = ""
    refutes_leading: bool = False
    strongest_counter: str = ""
    recommended_leading: str = ""


class _WireReviews(BaseModel):
    model_config = {"extra": "ignore"}
    reviews: list[_WireReview] = Field(default_factory=list)


@dataclass(frozen=True)
class AdversarialResult:
    judgment_id: str
    refutes_leading: bool
    strongest_counter: str
    recommended_leading: str = ""


async def adversarial_review(
    *, llm: LLMClient, judgments: tuple[KeyJudgment, ...]
) -> dict[str, AdversarialResult]:
    """全 judgment を 1 バッチで red-team レビューし judgment_id → 結果 を返す。"""
    if not judgments:
        return {}
    prompt = _render("synthesis_adversarial.j2", judgments=judgments)
    parsed = await llm.generate_structured(
        prompt, _WireReviews, temperature=_TEMPERATURE, max_tokens=_MAX_TOKENS, think=False
    )
    out: dict[str, AdversarialResult] = {}
    for r in parsed.reviews:
        if not r.judgment_id:
            continue
        out[r.judgment_id] = AdversarialResult(
            judgment_id=r.judgment_id,
            refutes_leading=bool(r.refutes_leading),
            strongest_counter=r.strongest_counter.strip()[:400],
            recommended_leading=r.recommended_leading.strip(),
        )
    return out


def apply_adversarial(j: KeyJudgment, r: AdversarialResult | None) -> KeyJudgment:
    """red-team 結果を judgment に適用 (決定論)。

    - 反証成立 (refutes_leading): 確度を low に降格。recommended_leading が当該 judgment の
      採点済み仮説なら leading を flip (方向は問わない = 対称)。
    - flip 規律 (2026-07-25 ACH 整合監査): 証拠採点で反証済み (反整合 > 整合) の仮説への
      flip は見送る — ACH の算術 (leading = 最少反整合) に反する主説交代を red-team の
      一存で許すと、証拠ゼロ仮説が公式見立てになる (ANCHOR-CI 事案 rev13)。反証の効果は
      確度降格 + 対立論拠の記録に留める (方向中立の規律)。
    - flip 時はマトリクスの verdict を新 leading で導出し直す (スカラとマトリクスの
      二重表現を書込前に必ず一致させる — 整合不変量)。
    - 反証不成立でも strongest_counter は adversarial_note に記録 (透明性)。
    """
    if r is None:
        return j
    note = r.strongest_counter
    if not r.refutes_leading:
        return replace(j, adversarial_refuted=False, adversarial_note=note)

    scored = {h.hypothesis: h for h in j.hypotheses}
    new_leading = j.leading_hypothesis
    candidate = scored.get(r.recommended_leading) if r.recommended_leading else None
    if candidate is not None:
        if is_counts_refuted(candidate):
            _log.warning(
                "adversarial_flip_suppressed",
                judgment=j.id,
                recommended=r.recommended_leading,
                consistent=candidate.consistent,
                inconsistent=candidate.inconsistent,
            )
        else:
            new_leading = r.recommended_leading
    basis = j.confidence_basis
    basis = f"{basis}; 敵対的検証で反証→low" if basis else "敵対的検証で反証→low"
    return replace(
        j,
        leading_hypothesis=new_leading,
        hypotheses=reconcile_hypotheses(j.hypotheses, new_leading),
        confidence="low",
        confidence_basis=basis,
        adversarial_refuted=True,
        adversarial_note=note,
    )
