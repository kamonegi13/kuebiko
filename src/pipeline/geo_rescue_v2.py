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
from dataclasses import dataclass
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
    if prompt_mode == "pir":
        pir_rescued = flat_rescued  # 本番の判定がそのまま pir の判定
    elif not flat_rescued:
        pir = await ArticleTriage(rescue_llm, think=think, force_pir=True).triage(article)
        if pir.error:
            return _fallback(flat, "pir_error", keep=keep)
        pir_rescued = pir.importance in keep
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
        return await asyncio.wait_for(
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


def log_outcome(article_id: str, outcome: V2Outcome, *, mode: str) -> None:
    """ログ (記事 id・判定・手掛かりの種類だけ。題・本文は出さない)。"""
    if outcome.gate_reason is not None:
        _log.info("geo_rescue_v2_gate", article_id=article_id, reason=outcome.gate_reason)
    if outcome.fallback is not None:
        _log.info("geo_rescue_v2_fallback", article_id=article_id, reason=outcome.fallback)
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
