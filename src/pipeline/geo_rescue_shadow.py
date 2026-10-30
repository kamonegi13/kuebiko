# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""地政学の救済の判定 v2 の影の記録 (``GEO_RESCUE_JUDGED_SHADOW=1``、2026-10-31)。

本番の救済 (flat) は**変えない**。毎時の triage の地政学の救済で、救済の候補 (triage が地政学を理由に
low にした記事) ごとに、本番の flat の判定に加えて pir の判定・語の関門の手掛かり・QW/QC/QD の確率を
取り、v2 の最終判定 (``geo_rescue_judge.judged_rescued``) と並べて ``geo_rescue_shadow`` に 1 行残す。
後で盲検の標本と比べる (事前登録 ``docs/replay_prereg/geo_rescue_shadow.md``)。

- 追加の LLM の負荷は 1 回の上限の件数 (``GEO_RESCUE_SHADOW_PER_RUN``、既定 30) と時間の予算
  (``GEO_RESCUE_SHADOW_BUDGET_SECONDS``、既定 180 秒) で抑える。超えた候補は状態だけ記録する
  (``cap_skipped`` / ``budget_skipped``) — 標本の枠から外した行を数えるため
- 失敗は triage に一切影響しない (例外は握って状態 ``error`` / 記録の失敗はログだけ)
- 救済のモデルは本番の救済と同じ client (``step:triage_geo_rescue``)、問いは ``Step.GEO_RESCUE_JUDGE``
  (救済の割当を継承)。モデルの digest は Ollama の ``/api/tags`` を 1 回の実行につき 1 度だけ読む
- 問いは本番の v2 と同じ候補 (flat が救わず pir が救い、手掛かりのある記事) にだけ当てる。ほかの候補は
  pir の判定と手掛かりだけ (負荷と判定を本番の v2 と同じにする)
- 本文・題は記録しない。判定の入力は種類 (``body`` / ``summary``) と sha256 (題 + 本文プレビュー) だけ
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.logging_config import get_logger
from src.pipeline.geo_rescue_cues import geo_cue_reasons
from src.pipeline.geo_rescue_judge import GeoJudgeResult, judge_geo_rescue, judged_rescued
from src.storage.repo_geo_rescue_shadow import (
    STATUS_BUDGET,
    STATUS_CAP,
    STATUS_ERROR,
    STATUS_OK,
    GeoRescueShadowRow,
)

if TYPE_CHECKING:
    from src.config_loader import AppConfig
    from src.tools.article_model import Article
    from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

SHADOW_ENV = "GEO_RESCUE_JUDGED_SHADOW"
PER_RUN_ENV = "GEO_RESCUE_SHADOW_PER_RUN"
BUDGET_ENV = "GEO_RESCUE_SHADOW_BUDGET_SECONDS"
DEFAULT_PER_RUN = 30
DEFAULT_BUDGET_SECONDS = 180.0
#: 救済の判定が medium 以上なら救う (本番の救済と同じ)
KEEP = frozenset({"medium", "high"})
#: ``/api/tags`` を読む timeout (失敗は digest なしで続ける)
TAGS_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class GeoRescueCandidate:
    """救済の候補 1 件と、本番の flat の判定 (``error`` = 判定の失敗)。"""

    article: Article
    flat: str


def is_geo_rescue_shadow_enabled() -> bool:
    return os.environ.get(SHADOW_ENV, "0").strip() == "1"


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def cue_types(cues: tuple[str, ...]) -> tuple[str, ...]:
    """手掛かり (``nation:CN`` 等) の種類 (``nation``) を重複なしで並べる。純関数。"""
    return tuple(dict.fromkeys(c.split(":", 1)[0] for c in cues))


def input_sha256(title: str, text: str) -> str:
    """判定の入力 (題 + 本文プレビュー) の sha256。正解付けの入力の照合に使う。純関数。"""
    return hashlib.sha256(f"{title}\n\n{text}".encode()).hexdigest()


def _skipped(
    c: GeoRescueCandidate, status: str, model: str | None, digest: str | None
) -> GeoRescueShadowRow:
    return GeoRescueShadowRow(
        article_id=c.article.id,
        flat=c.flat,
        status=status,
        model=model,
        model_digest=digest,
        flat_error=c.flat == "error",
    )


async def model_digest(base_url: str, model: str | None) -> str | None:
    """Ollama の ``/api/tags`` から ``model`` の digest (読めなければ None)。"""
    if not model:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=TAGS_TIMEOUT_SECONDS) as client:
            resp = await client.get(base_url.rstrip("/") + "/api/tags")
            resp.raise_for_status()
            for m in resp.json().get("models", []):
                if m.get("name") == model:
                    return str(m.get("digest") or "") or None
    except Exception as e:  # noqa: BLE001 — digest が読めなくても記録は続ける
        _log.debug("geo_rescue_shadow_digest_failed", error=str(e)[:120])
    return None


async def judge_candidate(
    c: GeoRescueCandidate,
    *,
    rescue_llm: LLMClient,
    config: AppConfig,
    think: bool,
    model: str | None,
    digest: str | None,
) -> GeoRescueShadowRow:
    """1 候補に pir の判定と v2 の判定を当てる (本番と同じ式・同じ問い)。"""
    from src.tools.article_triage import ArticleTriage

    pir_t = ArticleTriage(rescue_llm, think=think, force_pir=True)
    p = await pir_t.triage(c.article)
    pir = "error" if p.error else p.importance
    text = pir_t._triage_content(c.article)
    title = c.article.title or ""
    pir_error = pir == "error"
    flat_rescued, pir_rescued = c.flat in KEEP, pir in KEEP
    if flat_rescued or not pir_rescued:
        # 本番の v2 が問いを当てない候補 (flat が救う / pir が救わない)。手掛かりだけ記録し
        # 問いは当てない (負荷を本番の v2 と同じにする)。判定は式どおり flat の結果
        judge = GeoJudgeResult(
            cues=geo_cue_reasons(title, text), p_watched_action=None, p_allied_response=None
        )
        failed = False
    else:
        judge = await judge_geo_rescue(title, text, config=config)
        failed = judge.failed
    status = STATUS_ERROR if pir_error or failed else STATUS_OK
    judged = None
    if status == STATUS_OK:
        judged = judged_rescued(flat_rescued=flat_rescued, pir_rescued=pir_rescued, judge=judge)
    return GeoRescueShadowRow(
        article_id=c.article.id,
        flat=c.flat,
        pir=pir,
        status=status,
        cue_types=cue_types(judge.cues),
        p_qw=judge.p_watched_action,
        p_qc=judge.p_allied_response,
        p_qd=judge.p_cyber_geo,
        judged=judged,
        model=model,
        model_digest=digest,
        flat_error=c.flat == "error",
        pir_error=pir_error,
        judge_failed=failed,
        input_kind="body" if c.article.body_text else "summary",
        input_sha256=input_sha256(title, text),
    )


async def run_geo_rescue_shadow(
    candidates: list[GeoRescueCandidate],
    *,
    rescue_llm: LLMClient,
    config: AppConfig,
    think: bool = False,
) -> list[GeoRescueShadowRow]:
    """候補を順に (1 件ずつ) 判定する。上限の件数・時間の予算を超えた候補は状態だけ。

    例外は外へ出さない (1 候補の失敗は ``error``、予期しない失敗は空の結果)。
    """
    if not candidates:
        return []
    per_run = _env_int(PER_RUN_ENV, DEFAULT_PER_RUN)
    deadline = time.monotonic() + _env_float(BUDGET_ENV, DEFAULT_BUDGET_SECONDS)
    model = getattr(rescue_llm, "model", None)
    model = str(model) if model else None
    digest = await model_digest(config.ollama_base_url, model)
    rows: list[GeoRescueShadowRow] = []
    for i, c in enumerate(candidates):
        if i >= per_run:
            rows.append(_skipped(c, STATUS_CAP, model, digest))
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            rows.append(_skipped(c, STATUS_BUDGET, model, digest))
            continue
        try:
            rows.append(
                await asyncio.wait_for(
                    judge_candidate(
                        c,
                        rescue_llm=rescue_llm,
                        config=config,
                        think=think,
                        model=model,
                        digest=digest,
                    ),
                    timeout=remaining,
                )
            )
        except TimeoutError:
            rows.append(_skipped(c, STATUS_BUDGET, model, digest))
        except Exception as e:  # noqa: BLE001 — 影の記録の失敗は triage に影響させない
            _log.warning("geo_rescue_shadow_candidate_failed", error=type(e).__name__)
            rows.append(_skipped(c, STATUS_ERROR, model, digest))
    _log.info(
        "geo_rescue_shadow",
        candidates=len(candidates),
        judged=sum(1 for r in rows if r.status == STATUS_OK),
        judged_yes=sum(1 for r in rows if r.judged),
        skipped=sum(1 for r in rows if r.status in (STATUS_CAP, STATUS_BUDGET)),
        errors=sum(1 for r in rows if r.status == STATUS_ERROR),
    )
    return rows


__all__ = [
    "SHADOW_ENV",
    "GeoRescueCandidate",
    "cue_types",
    "input_sha256",
    "is_geo_rescue_shadow_enabled",
    "run_geo_rescue_shadow",
]
