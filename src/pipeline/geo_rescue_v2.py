"""地政学の救済 v2 を本番の救済の経路へ組み込む (GEO-RESCUE-JUDGED-IMPL-V2、2026-11-16)。

旗 ``GEO_RESCUE_V2`` = ``0`` (既定・従来どおり) / ``shadow`` (v2 を計算してログ
``geo_rescue_v2_shadow`` だけ。本番の判定は不変) / ``1`` (救済の判定を v2 に替える)。

式は ``geo_rescue_judge`` の部品をそのまま使う (複製しない)::

    rescued = (flat AND (geo_cue OR passed)) OR (pir AND passed)     # judged_rescued_gated

- pir の判定は救済の prompt が flat のときだけ追加で取る (``ArticleTriage(force_pir=True)``)。
  flat が救った行は式の結果に pir が効かないので取らない。救済の prompt が既に pir なら
  本番の判定がそのまま pir の判定 (影の記録と同じ扱い)
- 問いは手掛かりの種類に応じた分だけ (``judge_geo_rescue``)。flat が救い A〜C の手掛かりがある行は
  問わずに救う
- 判定不能・LLM の失敗・時間切れ・run の残り時間不足は **flat の判定に倒す** (従来どおりの側)
- 救う記事は medium 止まり・analysis_only の扱いは呼び出し側 (``filters``) の既存のまま
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from src.logging_config import get_logger
from src.pipeline.geo_rescue_cues import geo_cue_reasons
from src.pipeline.geo_rescue_judge import GeoJudgeResult, judge_geo_rescue, judged_rescued_gated

if TYPE_CHECKING:
    from src.config_loader import AppConfig
    from src.tools.article_model import Article
    from src.tools.article_triage import TriageDecision
    from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

V2_ENV = "GEO_RESCUE_V2"
MIN_REMAINING_ENV = "GEO_RESCUE_V2_MIN_REMAINING_SECONDS"
TIMEOUT_ENV = "GEO_RESCUE_V2_TIMEOUT_SECONDS"
#: 学習版 (union) の 1 候補の上限。v2 とは別に数える (超えたら v2 の結果のまま)
LEARNED_TIMEOUT_ENV = "GEO_RESCUE_LEARNED_TIMEOUT_SECONDS"
DEFAULT_LEARNED_TIMEOUT_SECONDS = 60.0
#: run の残り時間 (記事ループの soft deadline まで) がこれ未満なら v2 を飛ばす
DEFAULT_MIN_REMAINING_SECONDS = 150.0
#: 1 候補の v2 (pir の判定 + 問い 0〜3 回) の上限。超えたら flat に倒す
DEFAULT_TIMEOUT_SECONDS = 90.0

REMAINING_FN = Callable[[], float | None]


def geo_rescue_v2_mode() -> str:
    """``0`` / ``shadow`` / ``1``。未知の値は ``0``。"""
    raw = os.environ.get(V2_ENV, "0").strip().lower()
    return raw if raw in ("shadow", "1") else "0"


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class V2Outcome:
    """1 候補の v2 の結果。``fallback`` があれば v2 は使えず flat に倒す (``rescued`` は無効)。"""

    flat_rescued: bool
    pir_rescued: bool | None
    rescued: bool
    reason_text: str
    cue_kinds: tuple[str, ...]
    fallback: str | None = None
    gate_reason: str | None = None
    #: v2 が使った pir の判定 (low/medium/high、取っていなければ None) と問いの結果
    #: (学習版で使い回す)
    pir_level: str | None = None
    judge: GeoJudgeResult | None = None
    #: 学習版 (GEO_RESCUE_LEARNED=union) の P(救う)。当てていなければ None
    learned_p: float | None = None


def _kinds(cues: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(c.split(":", 1)[0] for c in cues))


def _gate_reason(
    *, flat_rescued: bool, pir_rescued: bool, judge: GeoJudgeResult, rescued: bool
) -> str | None:
    """v2 が救わなかった (flat か pir が救っていた) 理由。救った・どちらも救わないなら ``None``。"""
    if rescued or not (flat_rescued or pir_rescued):
        return None
    prefix = "flat" if flat_rescued else "pir"
    if not judge.cues:
        return f"{prefix}_no_cue"
    return f"{prefix}_questions_no"


def _fallback(flat: TriageDecision, reason: str, *, keep: set[str]) -> V2Outcome:
    return V2Outcome(
        flat_rescued=not flat.error and flat.importance in keep,
        pir_rescued=None,
        rescued=False,
        reason_text=flat.reason,
        cue_kinds=(),
        fallback=reason,
    )


async def _decide(
    article: Article,
    flat: TriageDecision,
    *,
    preview: str,
    prompt_mode: str,
    rescue_llm: LLMClient,
    think: bool,
    config: AppConfig,
    keep: set[str],
) -> V2Outcome:
    from src.tools.article_triage import ArticleTriage

    title = article.title or ""
    flat_rescued = flat.importance in keep
    reason_text = flat.reason
    pir_rescued = False
    pir_level: str | None = None
    if prompt_mode == "pir":
        pir_rescued = flat_rescued  # 本番の判定がそのまま pir の判定
        pir_level = flat.importance
    elif not flat_rescued:
        pir = await ArticleTriage(rescue_llm, think=think, force_pir=True).triage(article)
        if pir.error:
            return _fallback(flat, "pir_error", keep=keep)
        pir_rescued = pir.importance in keep
        pir_level = pir.importance
        reason_text = pir.reason
    cues = geo_cue_reasons(title, preview)
    judge = GeoJudgeResult(cues=cues, p_watched_action=None, p_allied_response=None)
    needs_questions = pir_rescued or (flat_rescued and not judge.has_geo_cue)
    if needs_questions and cues:
        judge = await judge_geo_rescue(title, preview, config=config)
        if judge.failed and not judge.passed:
            return _fallback(flat, "judge_failed", keep=keep)
    rescued = judged_rescued_gated(flat_rescued=flat_rescued, pir_rescued=pir_rescued, judge=judge)
    return V2Outcome(
        flat_rescued=flat_rescued,
        pir_rescued=pir_rescued,
        rescued=rescued,
        reason_text=reason_text,
        cue_kinds=_kinds(cues),
        gate_reason=_gate_reason(
            flat_rescued=flat_rescued, pir_rescued=pir_rescued, judge=judge, rescued=rescued
        ),
        pir_level=pir_level,
        judge=judge,
    )


async def decide_v2(
    article: Article,
    flat: TriageDecision,
    *,
    preview: str,
    prompt_mode: str,
    rescue_llm: LLMClient,
    think: bool,
    config: AppConfig,
    keep: set[str],
    remaining: REMAINING_FN | None = None,
) -> V2Outcome:
    """v2 の判定。例外・時間切れ・残り時間不足・flat の失敗は ``fallback`` つきで flat に倒す。"""
    if flat.error:
        return _fallback(flat, "flat_error", keep=keep)
    left = remaining() if remaining is not None else None
    if left is not None and left < _env_float(MIN_REMAINING_ENV, DEFAULT_MIN_REMAINING_SECONDS):
        return _fallback(flat, "low_budget", keep=keep)
    try:
        outcome = await asyncio.wait_for(
            _decide(
                article,
                flat,
                preview=preview,
                prompt_mode=prompt_mode,
                rescue_llm=rescue_llm,
                think=think,
                config=config,
                keep=keep,
            ),
            timeout=_env_float(TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS),
        )
    except TimeoutError:
        return _fallback(flat, "timeout", keep=keep)
    except Exception as e:  # noqa: BLE001  v2 の失敗で本番の救済を止めない
        _log.warning("geo_rescue_v2_failed", article_id=article.id, error=type(e).__name__)
        return _fallback(flat, "error", keep=keep)
    return await _with_learned_union(
        article, flat, outcome, preview=preview, rescue_llm=rescue_llm, think=think, config=config
    )


async def _with_learned_union(
    article: Article,
    flat: TriageDecision,
    outcome: V2Outcome,
    *,
    preview: str,
    rescue_llm: LLMClient,
    think: bool,
    config: AppConfig,
) -> V2Outcome:
    """GEO_RESCUE_LEARNED=union: v2 が救わなかった候補に学習版を当て、救えば救う (v2 ∨ 学習版)。

    学習版の失敗・時間切れは v2 の結果をそのまま返す (v2 を巻き込んで flat に倒さない)。
    """
    from src.pipeline.geo_rescue_learned import learned_mode

    if learned_mode() != "union" or outcome.rescued or outcome.fallback is not None:
        return outcome
    try:
        p = await asyncio.wait_for(
            _learned_probability(
                article,
                flat,
                outcome,
                preview=preview,
                rescue_llm=rescue_llm,
                think=think,
                config=config,
            ),
            timeout=_env_float(LEARNED_TIMEOUT_ENV, DEFAULT_LEARNED_TIMEOUT_SECONDS),
        )
    except Exception as e:  # noqa: BLE001 — 学習版の失敗で本番の救済を止めない
        _log.warning(
            "geo_rescue_learned_union_failed", article_id=article.id, error=type(e).__name__
        )
        return outcome
    if p is None:
        return outcome
    return replace(outcome, rescued=p >= _learned_threshold(), learned_p=p)


def _learned_threshold() -> float:
    from src.pipeline.geo_rescue_learned import load_model

    model = load_model()
    return model.threshold if model is not None else 1.1  # モデルが無ければ救わない


async def _learned_probability(
    article: Article,
    flat: TriageDecision,
    outcome: V2Outcome,
    *,
    preview: str,
    rescue_llm: LLMClient,
    think: bool,
    config: AppConfig,
) -> float | None:
    """学習版の P(救う)。学習と同じ特徴 (pir/flat・手掛かり・3 問の確率・埋め込み)。

    足りない分だけ取る。
    """
    import src.pipeline.geo_rescue_learned as gl
    from src.tools.article_triage import ArticleTriage
    from src.tools.embedding_client import OllamaEmbeddingClient

    model = gl.load_model()
    if model is None:
        return None
    title = article.title or ""
    pir_level = outcome.pir_level
    if pir_level is None:  # flat が救ったため pir を取っていない行
        pir = await ArticleTriage(rescue_llm, think=think, force_pir=True).triage(article)
        if pir.error:
            return None
        pir_level = pir.importance
    cues = geo_cue_reasons(title, preview)
    judge = outcome.judge
    qs: tuple[float | None, float | None, float | None] = (None, None, None)
    if cues:
        if judge is not None:
            qs = (judge.p_watched_action, judge.p_allied_response, judge.p_cyber_geo)
        if any(q is None for q in qs):
            j = await judge_geo_rescue(title, preview, config=config, probe_all=True)
            qs = (j.p_watched_action, j.p_allied_response, j.p_cyber_geo)
        if any(q is None for q in qs):
            return None
    emb = await OllamaEmbeddingClient(
        base_url=config.ollama_base_url, model=model.embedding_model
    ).embed(gl.embedding_input(title, preview), kind="document")
    feats = gl.build_features(
        pir=pir_level,
        flat=flat.importance,
        cues=cues,
        p_qw=qs[0],
        p_qc=qs[1],
        p_qd=qs[2],
        is_summary=not article.body_text,  # 影の記録と同じ決め方 (geo_rescue_shadow)
    )
    return gl.predict_proba(model, emb.vector, feats)


def log_outcome(article_id: str, outcome: V2Outcome, *, mode: str) -> None:
    """ログ (記事 id・判定・手掛かりの種類だけ。題・本文は出さない)。"""
    if outcome.gate_reason is not None:
        _log.info("geo_rescue_v2_gate", article_id=article_id, reason=outcome.gate_reason)
    if outcome.fallback is not None:
        _log.info("geo_rescue_v2_fallback", article_id=article_id, reason=outcome.fallback)
    if outcome.learned_p is not None:
        # union: v2 が救わなかった候補への学習版の判定 (救ったかどうかと確率だけ)
        _log.info(
            "geo_rescue_learned_union",
            article_id=article_id,
            p_ml=round(outcome.learned_p, 4),
            rescued=outcome.rescued,
        )
    if mode == "shadow":
        _log.info(
            "geo_rescue_v2_shadow",
            article_id=article_id,
            flat=outcome.flat_rescued,
            pir=outcome.pir_rescued,
            v2=None if outcome.fallback else outcome.rescued,
            gate_dropped=outcome.gate_reason is not None and outcome.flat_rescued,
            fallback=outcome.fallback,
            cue_kinds=list(outcome.cue_kinds),
        )


__all__ = ["V2Outcome", "decide_v2", "geo_rescue_v2_mode", "log_outcome"]
