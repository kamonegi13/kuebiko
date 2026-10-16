"""detect が束ねた記事の記録 (M2、2026-10-16)。

本番は detect の ``article_ids`` を保存していなかった (``situation_detection_log`` は開設の先頭
証拠 1 件だけ)。LLM の束ね → 後処理 (和集合・``_unbundle_detected``) 後の塊 → 開設/合流/見送り
を ``detect_bundles`` に残し、束ねの正解づくりと分解 (``DETECT_DECOMPOSE``) の比較に使う。
**記録だけで挙動は変えない**。純粋関数 (DB に触れない) と記録の型だけを置く。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Literal

from src.synthesis.grounded.incremental import DetectResult

Variant = Literal["current", "decompose_shadow", "decompose"]
#: shadow_open = 分けた形 (shadow) が開設候補にしたもの (本番では開設していない)
Outcome = Literal["opened", "merged", "skipped", "pending", "shadow_open"]
#: 記録する claim 文・理由の上限 (台帳の log_detection と同程度)
_CLAIM_MAX = 300
_REASON_MAX = 200

ORIGIN_LLM = "llm"
ORIGIN_UNION = "ml_union"
ORIGIN_UNBUNDLED = "unbundled"
ORIGIN_DECOMPOSE = "decompose"
ORIGIN_DROPPED = "llm_dropped"


@dataclass(frozen=True)
class BundleRecord:
    """detect_bundles の 1 行。outcome は開設ループの結果で後から確定する。"""

    run_at: str
    variant: Variant
    claim_id: str
    origin: str
    claim: str
    domain: str
    llm_article_ids: tuple[str, ...]
    final_article_ids: tuple[str, ...]
    outcome: Outcome = "pending"
    situation_id: str | None = None
    reason: str = ""
    probability: float | None = None
    model: str = ""

    @property
    def run_date(self) -> str:
        return self.run_at[:10]

    def with_outcome(
        self, outcome: Outcome, *, situation_id: str | None = None, reason: str = ""
    ) -> BundleRecord:
        return replace(
            self, outcome=outcome, situation_id=situation_id, reason=reason[:_REASON_MAX]
        )

    def to_row(self) -> tuple[object, ...]:
        return (
            self.run_at,
            self.run_date,
            self.variant,
            self.claim_id,
            self.origin,
            self.claim[:_CLAIM_MAX],
            self.domain,
            json.dumps(list(self.llm_article_ids), ensure_ascii=False),
            json.dumps(list(self.final_article_ids), ensure_ascii=False),
            self.outcome,
            self.situation_id,
            self.reason[:_REASON_MAX],
            self.probability,
            self.model,
        )


def make_claim_id(run_at: str, variant: str, article_ids: Iterable[str], claim: str) -> str:
    """run・方式・記事集合・claim から決まる短い id (同じ入力なら同じ id)。"""
    basis = "|".join([run_at, variant, ",".join(sorted(article_ids)), claim])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _origin_for(ids: set[str], llm_ids: tuple[str, ...], union_ids: set[str]) -> str:
    """LLM の束ねと同じ塊なら llm / 割り直しなら unbundled / 和集合で足されたなら ml_union。"""
    if llm_ids:
        return ORIGIN_LLM if set(llm_ids) == ids else ORIGIN_UNBUNDLED
    return ORIGIN_UNION if ids <= union_ids else ORIGIN_UNBUNDLED


def build_current_records(
    *,
    run_at: str,
    raw_llm: DetectResult,
    after_union: DetectResult,
    final: DetectResult,
    model: str = "",
) -> list[BundleRecord]:
    """本番の detect の束ねの記録を作る (純粋関数)。

    ``raw_llm`` = LLM の出力そのまま / ``after_union`` = ML 和集合を足した後 /
    ``final`` = ``_unbundle_detected`` 後 (これが開設ループへ渡る塊)。各 final の塊に、記事が
    重なる LLM の束ねの ``article_ids`` を付ける。final に残らなかった LLM の束ね (関門で消えた)
    は origin=llm_dropped / outcome=skipped で残す。
    """
    union_ids = {a for c in after_union.open for a in c.article_ids}
    records: list[BundleRecord] = []
    covered: set[int] = set()
    for c in final.open:
        ids = set(c.article_ids)
        parents = [i for i, r in enumerate(raw_llm.open) if ids & set(r.article_ids)]
        covered.update(parents)
        llm_ids = tuple(dict.fromkeys(a for i in parents for a in raw_llm.open[i].article_ids))
        records.append(
            BundleRecord(
                run_at=run_at,
                variant="current",
                claim_id=make_claim_id(run_at, "current", c.article_ids, c.claim),
                origin=_origin_for(ids, llm_ids, union_ids),
                claim=c.claim,
                domain=c.domain,
                llm_article_ids=llm_ids,
                final_article_ids=tuple(c.article_ids),
                model=model,
            )
        )
    for i, r in enumerate(raw_llm.open):
        if i in covered:
            continue
        records.append(
            BundleRecord(
                run_at=run_at,
                variant="current",
                claim_id=make_claim_id(run_at, "current", r.article_ids, r.claim),
                origin=ORIGIN_DROPPED,
                claim=r.claim,
                domain=r.domain,
                llm_article_ids=tuple(r.article_ids),
                final_article_ids=(),
                outcome="skipped",
                reason="束ねの関門で開設せず (書き直しの結果なし)",
                model=model,
            )
        )
    return records


def resolve_outcomes(
    records: Iterable[BundleRecord],
    outcomes: Mapping[str, tuple[Outcome, str | None, str]],
) -> list[BundleRecord]:
    """claim_id → (outcome, situation_id, reason) で pending の行を確定する (新しいリストを返す)。

    開設ループが一度も触れなかった pending は ``skipped`` (開設ループ前に消えた) にする。
    """
    out: list[BundleRecord] = []
    for r in records:
        if r.outcome != "pending":
            out.append(r)
            continue
        got = outcomes.get(r.claim_id)
        if got is None:
            out.append(
                r.with_outcome(
                    "skipped", reason="開設ループが結果を記録しなかった (ACH 後の関門等で脱落)"
                )
            )
        else:
            out.append(r.with_outcome(got[0], situation_id=got[1], reason=got[2]))
    return out
