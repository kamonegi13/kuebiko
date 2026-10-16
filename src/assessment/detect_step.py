"""detect の決定 (開設候補・合流) と束ねの記録を 1 つの段にまとめる (M2、2026-10-16)。

``build_estimate_stateful`` の「LLM detect → ML 和集合 → 束ねの関門」と、分けた形
(``DETECT_DECOMPOSE``、:mod:`src.assessment.detect_decompose`) の切替をここに置く。入力は不変の
``DetectContext``、出力は不変の ``DetectDecision``。DB へ書くのは ML の shadow 記録
(従来どおり) だけで、合流は **書かずに** ``merges`` で返す (呼び出し側が通常の割当に足し、
同じ run の増分 ACH で評価される。休眠からの復帰も通常の割当と同じ経路)。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from src.assessment.assign_gate import gate_mode
from src.assessment.assignment import ArticleKeys, SituationKeys
from src.assessment.detect_bundle_log import (
    BundleRecord,
    Variant,
    build_current_records,
)
from src.assessment.detect_decompose import (
    SHADOW_BUDGET_SECONDS,
    SHADOW_MAX_CLAIMS,
    DecomposeInputs,
    DecomposeResult,
    decompose_mode,
    merged_assignments,
    run_decompose,
    to_detect_result,
    to_records,
)
from src.assessment.situation_store import SituationStore
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.synthesis.grounded.incremental import DetectResult, detect_new_claims
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)


@dataclass(frozen=True)
class DetectContext:
    """detect の決定に要る本番の部品 (読み取り専用の束)。"""

    llm_input: Sequence[dict[str, object]]  # detect ML で絞った後の候補
    detect_input: Sequence[dict[str, object]]  # 絞る前の候補 (束ねの関門が参照)
    ml_scores: Mapping[str, float]
    ml_kinds: Mapping[str, str]
    entities_by_id: Mapping[str, set[str]]
    sit_keys: Sequence[SituationKeys]
    art_keys: Mapping[str, ArticleKeys]
    repo: RunHistoryRepository
    store: SituationStore
    llm: LLMClient
    active_titles: list[str]
    pir_context: list[dict[str, str]]
    period_label: str
    now_iso: str

    @property
    def model_label(self) -> str:
        return str(getattr(self.llm, "model", ""))

    def decompose_inputs(self) -> DecomposeInputs:
        return DecomposeInputs(
            candidates=self.llm_input,
            ml_scores=self.ml_scores,
            entities_by_id=self.entities_by_id,
            situation_keys=self.sit_keys,
            art_keys=self.art_keys,
            repo=self.repo,
            store=self.store,
            llm=self.llm,
            active_titles=self.active_titles,
            pir_context=self.pir_context,
            period_label=self.period_label,
        )


@dataclass(frozen=True)
class DetectDecision:
    detected: DetectResult
    bundle_records: tuple[BundleRecord, ...]
    bundle_variant: Variant
    #: mode=1 で合流と判定した 情勢 id → 記事 id (DB 未書込。通常の割当に足して使う)
    merges: Mapping[str, list[str]]


async def decide_detect(ctx: DetectContext) -> DetectDecision:
    """開設候補を決める。``DETECT_DECOMPOSE=1`` で分けた形が使えればそれ、無理なら従来の detect。"""
    decomposed = await _decomposed_safe(ctx) if decompose_mode() == "1" else None
    if decomposed is not None:
        return _from_decomposed(ctx, decomposed)
    return await _current(ctx)


def _from_decomposed(ctx: DetectContext, decomposed: DecomposeResult) -> DetectDecision:
    live = {k.row.situation_id for k in ctx.sit_keys}
    merges = {s: a for s, a in merged_assignments(decomposed).items() if s in live}
    _log.info(
        "detect_decompose_applied",
        clusters=len(decomposed.outcomes),
        opened=len(decomposed.opened()),
        merged_clusters=len(decomposed.merged()),
        merged_articles=sum(len(a) for a in merges.values()),
        overflow=decomposed.overflow,
    )
    return DetectDecision(
        detected=to_detect_result(decomposed),
        bundle_records=tuple(
            to_records(decomposed, run_at=ctx.now_iso, variant="decompose", model=ctx.model_label)
        ),
        bundle_variant="decompose",
        merges=merges,
    )


async def _decomposed_safe(ctx: DetectContext) -> DecomposeResult | None:
    """mode=1: 合流は割当の関門が ``on`` のときだけ。失敗・ML 採点なしは None (従来の detect)。"""
    try:
        return await run_decompose(
            ctx.decompose_inputs(),
            merge_enabled=gate_mode() == "on",
            persist=True,
            enforce_grounding=True,
        )
    except Exception as exc:  # noqa: BLE001 — 可用性に影響させない
        _log.warning("detect_decompose_failed", error=type(exc).__name__)
        return None


async def _current(ctx: DetectContext) -> DetectDecision:
    """従来の detect (LLM → ML 和集合 → ML shadow 記録 → 束ねの関門) と、束ねの記録。"""
    from src.assessment import stateful

    detected = await detect_new_claims(
        llm=ctx.llm,
        articles=list(ctx.llm_input),
        active_titles=ctx.active_titles,
        pir_context=ctx.pir_context,
        period_label=ctx.period_label,
    )
    raw_llm = detected
    # 和集合 (SYNTHESIS §51): LLM が選ばなかったもののうち ML の上位を足す。失敗しても本体を
    # 落とさない。⚠ 絞り込み **後** の候補から採る (replay が測ったのはこの経路)
    if ctx.ml_scores:
        try:
            detected = await stateful._add_ml_union_claims(  # noqa: SLF001
                detected=detected,
                llm=ctx.llm,
                detect_input=list(ctx.llm_input),
                ml_scores=dict(ctx.ml_scores),
                active_titles=ctx.active_titles,
                pir_context=ctx.pir_context,
                period_label=ctx.period_label,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("detect_ml_union_failed", error=type(exc).__name__)
    try:
        stateful._record_detect_ml_shadow(  # noqa: SLF001
            store=ctx.store,
            run_at=ctx.now_iso,
            scores=dict(ctx.ml_scores),
            kinds=dict(ctx.ml_kinds),
            llm_opened={a for c in detected.open for a in c.article_ids},
        )
    except Exception as exc:  # noqa: BLE001 — shadow は本体の可用性に影響させない
        _log.warning("detect_ml_shadow_failed", error=type(exc).__name__)
    after_union = detected
    # 束ねの関門: 別々の事案を 1 claim にまとめたら開設せず、塊ごとに書き直させる (2026-09-24)
    try:
        detected = await stateful._unbundle_detected(  # noqa: SLF001
            detected,
            repo=ctx.repo,
            entities_by_id=ctx.entities_by_id,
            detect_input=ctx.detect_input,
            llm=ctx.llm,
            active_titles=ctx.active_titles,
            pir_context=ctx.pir_context,
            period_label=ctx.period_label,
        )
    except Exception as exc:  # noqa: BLE001 — 関門の失敗で開設を止めない
        _log.warning("detect_bundle_gate_failed", error=type(exc).__name__)
    records: list[BundleRecord] = []
    try:
        records = build_current_records(
            run_at=ctx.now_iso,
            raw_llm=raw_llm,
            after_union=after_union,
            final=detected,
            model=ctx.model_label,
        )
    except Exception as exc:  # noqa: BLE001 — 記録の失敗で開設ループを落とさない
        _log.warning("detect_bundle_records_failed", error=type(exc).__name__)
    return DetectDecision(
        detected=detected, bundle_records=tuple(records), bundle_variant="current", merges={}
    )


async def shadow_records(ctx: DetectContext) -> list[BundleRecord]:
    """``DETECT_DECOMPOSE=shadow``: 分けた形の結果を記録用の行にする (本番の開設は変えない)。

    本番の開設ループの後ろで走らせる。追加の LLM 呼出は時間の予算 (``SHADOW_BUDGET_SECONDS``) と
    書き手の件数 (``SHADOW_MAX_CLAIMS``) で縛り、要約埋込は保存しない。タイムアウト・失敗は log に
    出して空を返す。外からのキャンセル (``CancelledError``) は握らず伝える。
    """
    if decompose_mode() != "shadow":
        return []
    try:
        res = await asyncio.wait_for(
            run_decompose(
                ctx.decompose_inputs(),
                merge_enabled=_gate_not_off(),
                persist=False,
                enforce_grounding=False,
                max_claims=SHADOW_MAX_CLAIMS,
            ),
            timeout=SHADOW_BUDGET_SECONDS,
        )
    except TimeoutError:
        _log.warning("detect_decompose_shadow_timeout", budget_seconds=SHADOW_BUDGET_SECONDS)
        return []
    except Exception as exc:  # noqa: BLE001 — shadow は本体の可用性に影響させない
        _log.warning("detect_decompose_shadow_failed", error=type(exc).__name__)
        return []
    if res is None:
        _log.info("detect_decompose_shadow_skipped", reason="no_ml_scores_or_candidates")
        return []
    _log.info(
        "detect_decompose_shadow",
        clusters=len(res.outcomes),
        opened=len(res.opened()),
        merged_clusters=len(res.merged()),
        violations=sum(1 for o in res.opened() if o.violations),
        overflow=res.overflow,
    )
    return to_records(res, run_at=ctx.now_iso, variant="decompose_shadow", model=ctx.model_label)


def _gate_not_off() -> bool:
    """shadow の合流の採点は割当の関門が off でなければ行う (本番の割当と同じ設定に従う)。"""
    return gate_mode() != "off"


def with_records(decision: DetectDecision, extra: Sequence[BundleRecord]) -> DetectDecision:
    """記録の行を足した新しい決定 (元は変えない)。"""
    return replace(decision, bundle_records=(*decision.bundle_records, *extra))
