"""段B: 増分 ACH (継続 Situation の判定更新) + 新規開設判断 (detect-new)。

増分 ACH は「前回判定 + 台帳の主要証拠抜粋 + 新着ソースのみ」を入力に対称再評価する —
毎 run ゼロから再導出することによる仮説フリップ不安定 (台帳蓄積で実測) を解消しつつ、
アンカリングは prompt の対称再評価指示 + adversarial 検証で防ぐ。台帳が何年育っても
prompt は有界 (既存証拠は上位抜粋のみ)。

detect-new は旧 nominate の後継: **未割当の残余のみ**を対象に、PIR と使命序列を明示注入して
「追跡を開く価値」を判断し、開かなかった high 記事には理由を残す (選定の監査可能性)。
設計: docs/synthesis_situation_ledger_design.md §3.2-3.3。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from src.logging_config import get_logger
from src.synthesis.grounded.estimate import EvidenceItem, HypothesisScore
from src.synthesis.grounded.hypotheses import (
    Hypothesis,
    hypotheses_for_domain,
    is_known_hypothesis,
)
from src.synthesis.grounded.passes import (
    _ATTRIBUTION_OPTIONS,
    _MAX_TOKENS,
    _TEMPERATURE,
    ClaimAnalysis,
    _norm_basis,
    _norm_conf,
    _norm_polarity,
    _render,
    _verdict,
    norm_claim_type,
)
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

# 増分 prompt に含める既存証拠抜粋の上限 (prompt 有界化。全証拠は DB にあり UI で参照可)
_PRIOR_EXCERPTS_MAX = 5
# 増分 ACH に再提示する監視指標の上限/Situation (持ち越しの prompt 有界化、2026-08-14)。
# 提示数の上限であると同時に fired_indicators の受理上限でもある (提示した分は
# すべて発火しうる)。active 情勢の open 予測は中央 6 / p90 22 / 最大 90 件で、
# 20 なら 9 割の情勢を全量カバーする。実測負荷は forecast.py の module docstring 参照。
CARRIED_INDICATORS_MAX = 20
_DETECT_OPEN_MAX = 5  # 1 run の新規開設上限 (超過分は log、翌 run に再浮上して回復可能)

# claim 改訂の文字化けガード (実測: 31B が改訂 claim に簡体字/拡張漢字を混入し
# 「最高弔导能ンエ异席」のような破損文を生成・保存した)。CJK 拡張 A/B は日本語文で
# 実質使われないため 1 文字でも含めば棄却。ひらがな不在も日本語文として不自然。
_CJK_EXT_RE = re.compile(r"[㐀-䶿\U00020000-\U0002a6df]")
_HIRAGANA_RE = re.compile(r"[ぁ-ん]")


def is_sane_japanese_claim(text: str) -> bool:
    """改訂 claim が日本語文として健全か (文字化け棄却の決定論ヒューリスティック)。"""
    if not text.strip():
        return False
    if _CJK_EXT_RE.search(text):
        return False
    return bool(_HIRAGANA_RE.search(text))


# ---------- 増分 ACH ----------


class _WireIncEvidence(BaseModel):
    model_config = {"extra": "ignore"}
    article_id: str = ""
    attribution_basis: str = "unattributed"
    excerpt: str = ""
    polarity: str = "neutral"


class _WireIncHypothesis(BaseModel):
    model_config = {"extra": "ignore"}
    hypothesis: str = ""
    consistent: int = 0
    inconsistent: int = 0


class _WireIncremental(BaseModel):
    model_config = {"extra": "ignore"}
    evidence: list[_WireIncEvidence] = Field(default_factory=list)
    hypotheses: list[_WireIncHypothesis] = Field(default_factory=list)
    leading_hypothesis: str = ""
    confidence: str = "low"
    claim: str = ""
    claim_type: str = ""
    implication: str = ""
    fired_indicators: list[str] = Field(default_factory=list)
    scope_expanded: bool = False
    key_assumptions: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    indicators: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class PriorJudgmentView:
    """増分 prompt に渡す前回判定の要約 (台帳の latest revision から構築)。"""

    claim: str
    claim_type: str
    leading_hypothesis: str
    confidence: str
    hypotheses: tuple[dict[str, object], ...]  # {hypothesis, consistent, inconsistent}
    indicators: tuple[str, ...]
    key_excerpts: tuple[dict[str, str], ...]  # {polarity, excerpt} 上位のみ


@dataclass(frozen=True)
class IncrementalAnalysis:
    """増分 ACH の結果。ClaimAnalysis + 段B 固有 (claim 改訂 / 指標発火 / 拡大)。"""

    analysis: ClaimAnalysis
    claim: str
    fired_indicators: tuple[str, ...]
    scope_expanded: bool


async def incremental_ground_and_score(
    *,
    llm: LLMClient,
    situation_title: str,
    prior: PriorJudgmentView,
    domain: str,
    sources: list[dict[str, str]],
    tier_by_id: dict[str, str],
    hypotheses_override: tuple[Hypothesis, ...] | None = None,
) -> IncrementalAnalysis:
    """前回判定 + 新着ソースで判定を増分更新する (対称再評価・ACH 集計は加算)。"""
    prompt = _render(
        "synthesis/ground_incremental.j2",
        situation_title=situation_title,
        prior=prior,
        sources=sources,
        attribution_options=_ATTRIBUTION_OPTIONS,
        hypotheses=hypotheses_override or hypotheses_for_domain(domain),
    )
    # max_attempts=1: 増分 ACH は 1 呼出が数百秒級で、切断 JSON の同条件リトライは
    # 時間だけ倍加する (2026-07-04 実測: 800s×2 で pipeline timeout)。失敗した Situation は
    # skip され翌 run の新着で再評価される (台帳設計の回復可能性) — リトライより skip が正。
    a = await llm.generate_structured(
        prompt,
        _WireIncremental,
        temperature=_TEMPERATURE,
        max_tokens=_MAX_TOKENS,
        think=False,
        max_attempts=1,
    )
    if is_known_hypothesis(a.leading_hypothesis):
        leading = a.leading_hypothesis
    else:
        # parse 欠落は前回 leading を維持 (増分更新で unverified に落とすと flip ノイズになる)
        _log.warning(
            "grounded_incremental_leading_unknown",
            situation=situation_title[:60],
            raw=a.leading_hypothesis[:40],
        )
        leading = prior.leading_hypothesis
    evidence = tuple(
        EvidenceItem(
            article_id=e.article_id,
            source_tier=tier_by_id.get(e.article_id, "unknown"),
            attribution_basis=_norm_basis(e.attribution_basis),
            excerpt=e.excerpt.strip()[:500],
            polarity=_norm_polarity(e.polarity),
        )
        for e in a.evidence
        if e.excerpt.strip()
    )
    hyps = tuple(
        HypothesisScore(
            hypothesis=h.hypothesis,
            consistent=max(0, h.consistent),
            inconsistent=max(0, h.inconsistent),
            verdict=_verdict(h, leading),  # type: ignore[arg-type]
        )
        for h in a.hypotheses
        if is_known_hypothesis(h.hypothesis)
    )
    analysis = ClaimAnalysis(
        evidence=evidence,
        hypotheses=hyps,
        leading_hypothesis=leading,
        llm_confidence=_norm_conf(a.confidence),
        key_assumptions=tuple(s.strip() for s in a.key_assumptions[:3] if s.strip()),
        missing_evidence=tuple(s.strip() for s in a.missing_evidence[:3] if s.strip()),
        indicators=tuple(s.strip() for s in a.indicators[:3] if s.strip()),
        claim_type=norm_claim_type(a.claim_type.strip() or prior.claim_type),
        implication=a.implication.strip()[:400],
    )
    revised = a.claim.strip()
    if revised and revised != prior.claim and not is_sane_japanese_claim(revised):
        _log.warning(
            "grounded_incremental_claim_garbled",
            situation=situation_title[:60],
            raw=revised[:60],
        )
        revised = ""  # 破損した改訂は棄却して前回 claim を維持
    return IncrementalAnalysis(
        analysis=analysis,
        claim=revised or prior.claim,
        # 発火は提示した指標数まで許す。持ち越し導入前は提示が最大 3 件だったため
        # [:3] で足りていたが、そのままだと 4 件目以降の発火を切り捨てて
        # 「照会したのに hit にならない」新たな誤判定を作る (2026-08-14)。
        fired_indicators=tuple(
            s.strip() for s in a.fired_indicators[:CARRIED_INDICATORS_MAX] if s.strip()
        ),
        scope_expanded=bool(a.scope_expanded),
    )


# ---------- 新規開設判断 (detect-new) ----------


class _WireOpenClaim(BaseModel):
    model_config = {"extra": "ignore"}
    claim: str = ""
    domain: str = ""
    article_ids: list[str] = Field(default_factory=list)


class _WireRejected(BaseModel):
    model_config = {"extra": "ignore"}
    article_id: str = ""
    reason: str = ""


class _WireDetectResult(BaseModel):
    model_config = {"extra": "ignore"}
    open: list[_WireOpenClaim] = Field(default_factory=list)
    rejected: list[_WireRejected] = Field(default_factory=list)


@dataclass(frozen=True)
class DetectedClaim:
    claim: str
    domain: str
    article_ids: tuple[str, ...]


@dataclass(frozen=True)
class DetectResult:
    open: tuple[DetectedClaim, ...]
    rejected: tuple[tuple[str, str], ...]  # (article_id, reason)
    overflow: int  # 上限で切った開設候補数 (no-silent-caps: 必ず log される)


async def detect_new_claims(
    *,
    llm: LLMClient,
    articles: list[dict[str, object]],
    active_titles: list[str],
    pir_context: list[dict[str, str]],
    period_label: str,
) -> DetectResult:
    """未割当残余から新規追跡を開く価値のある claim を選ぶ (PIR + 使命序列を明示注入)。"""
    if not articles:
        return DetectResult(open=(), rejected=(), overflow=0)
    prompt = _render(
        "synthesis/detect_new.j2",
        articles=articles,
        active_titles=active_titles,
        pir_context=pir_context,
        period_label=period_label,
    )
    r = await llm.generate_structured(
        prompt, _WireDetectResult, temperature=_TEMPERATURE, max_tokens=_MAX_TOKENS, think=False
    )
    valid_ids = {str(a.get("article_id", "")) for a in articles}
    opened: list[DetectedClaim] = []
    for c in r.open:
        claim = c.claim.strip()
        ids = tuple(i for i in c.article_ids if i in valid_ids)
        if claim and ids:
            opened.append(
                DetectedClaim(
                    claim=claim, domain=c.domain.strip() or "unclassified", article_ids=ids
                )
            )
    overflow = max(0, len(opened) - _DETECT_OPEN_MAX)
    if overflow:
        _log.warning("grounded_detect_new_overflow", proposed=len(opened), cap=_DETECT_OPEN_MAX)
    rejected = tuple(
        (x.article_id, x.reason.strip()[:200]) for x in r.rejected if x.article_id in valid_ids
    )
    return DetectResult(open=tuple(opened[:_DETECT_OPEN_MAX]), rejected=rejected, overflow=overflow)
