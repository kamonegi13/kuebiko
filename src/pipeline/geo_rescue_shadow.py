# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""地政学の救済の判定 v2 の影の記録 (``GEO_RESCUE_JUDGED_SHADOW=1``、2026-10-31)。

本番の救済 (flat) は**変えない**。2 段に分ける (毎時の収集の時間の予算を食わないため):

1. **triage の時点で写すだけ** (``pending_rows``): 救済の候補 (triage が地政学を理由に low にした記事) ごとに、
   本番の flat の判定・語の関門の手掛かりの種類 (決定論)・判定の入力 (題と本文プレビュー)・その sha256・
   救済の prompt の選択を ``pending`` の行にする。**LLM は呼ばない** (DB への書き込みは数 ms)
2. **毎時保守チェーンの段で判定する** (``judge_pending``、``ui/services/geo_rescue_shadow_job``):
   古い順に上限の件数・時間の予算まで、pir の判定と QW/QC/QD を取り、v2 の最終判定
   (``geo_rescue_judge.judged_rescued``) を書き込む。72 時間を過ぎた ``pending`` は ``expired``

- 救済の prompt が既に pir (``GEO_RESCUE_PROMPT=pir``) なら、本番の判定がそのまま pir の判定なので
  pir の生成を繰り返さない
- 問いは本番の v2 と同じ候補 (flat が救わず、pir が救い、手掛かりのある記事) にだけ当てる
- pir の判定のモデルは ``Step.TRIAGE_GEO_RESCUE``、問いのモデルは ``Step.GEO_RESCUE_JUDGE`` の解決先
  (救済の割当を継承) を、それぞれ記録する。digest は Ollama の ``/api/tags`` を 1 回の段につき 1 度だけ読む
- 判定の入力 (題・本文プレビュー) は DB にだけ残す (正解付けの入力)。**ログには出さない**
- 失敗は triage にも保守チェーンの他の段にも影響しない
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from src.logging_config import get_logger
from src.pipeline.geo_rescue_cues import geo_cue_reasons
from src.pipeline.geo_rescue_judge import GeoJudgeResult, judge_geo_rescue, judged_rescued
from src.storage.repo_geo_rescue_shadow import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PENDING,
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
DEFAULT_BUDGET_SECONDS = 150.0
#: ``pending`` のまま この時間を過ぎた行は判定しない (``expired``)。積み残しが際限なく増えないように
EXPIRE_HOURS = 72.0
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


# ---- 1. triage の時点: 写すだけ (LLM なし) ----


def pending_rows(
    candidates: list[GeoRescueCandidate], *, prompt_mode: str
) -> list[GeoRescueShadowRow]:
    """候補を ``pending`` の行にする (決定論・LLM なし)。入力は triage と同じ本文プレビュー。"""
    from src.tools.article_triage import ArticleTriage

    preview = ArticleTriage(cast(Any, _NoLlm()))._triage_content
    rows: list[GeoRescueShadowRow] = []
    for c in candidates:
        title = c.article.title or ""
        text = preview(c.article)
        rows.append(
            GeoRescueShadowRow(
                article_id=c.article.id,
                flat=c.flat,
                status=STATUS_PENDING,
                prompt_mode=prompt_mode,
                cue_types=cue_types(geo_cue_reasons(title, text)),
                flat_error=c.flat == "error",
                input_kind="body" if c.article.body_text else "summary",
                input_sha256=input_sha256(title, text),
                title=title,
                input_text=text,
                feed_title=c.article.feed_title or "",
                url=c.article.url,
            )
        )
    return rows


class _NoLlm:
    """本文プレビューを作るだけの ``ArticleTriage`` に渡す空の client (呼ばれない)。"""

    model = ""


# ---- 2. 保守チェーンの段: 判定 ----


def article_from_row(row: GeoRescueShadowRow) -> Article:
    """記録の行から判定に渡す記事を組み直す (題・フィード名・本文プレビュー)。"""
    from src.tools.article_model import Article

    return Article(
        id=row.article_id,
        url=row.url or "https://invalid.example/",
        title=row.title or "",
        summary_html="",
        body_text=row.input_text or None,
        published=datetime.now(UTC),
        feed_title=row.feed_title or "",
        feed_url="",
    )


async def model_digests(base_url: str, models: list[str | None]) -> dict[str, str | None]:
    """Ollama の ``/api/tags`` から各モデルの digest (読めなければ None)。"""
    names = {m for m in models if m}
    out: dict[str, str | None] = dict.fromkeys(names)
    if not names:
        return out
    try:
        import httpx

        async with httpx.AsyncClient(timeout=TAGS_TIMEOUT_SECONDS) as client:
            resp = await client.get(base_url.rstrip("/") + "/api/tags")
            resp.raise_for_status()
            for m in resp.json().get("models", []):
                if m.get("name") in names:
                    out[str(m["name"])] = str(m.get("digest") or "") or None
    except Exception as e:  # noqa: BLE001 — digest が読めなくても判定は続ける
        _log.debug("geo_rescue_shadow_digest_failed", error=type(e).__name__)
    return out


@dataclass(frozen=True)
class JudgeContext:
    rescue_llm: LLMClient
    config: AppConfig
    think: bool
    model: str | None
    model_digest: str | None
    judge_model: str | None
    judge_model_digest: str | None


async def judge_row(row: GeoRescueShadowRow, ctx: JudgeContext) -> GeoRescueShadowRow:
    """1 行に pir の判定と v2 の判定を当てる (本番と同じ式・同じ問い)。"""
    from src.tools.article_triage import ArticleTriage

    article = article_from_row(row)
    title, text = row.title or "", row.input_text or ""
    if row.prompt_mode == "pir":
        pir = row.flat  # 本番の救済が既に pir の prompt で判定している (生成を繰り返さない)
    else:
        p = await ArticleTriage(ctx.rescue_llm, think=ctx.think, force_pir=True).triage(article)
        pir = "error" if p.error else p.importance
    pir_error = pir == "error"
    flat_rescued, pir_rescued = row.flat in KEEP, pir in KEEP
    if flat_rescued or not pir_rescued:
        # 本番の v2 が問いを当てない候補。手掛かりだけで、判定は式どおり flat の結果
        judge = GeoJudgeResult(
            cues=geo_cue_reasons(title, text), p_watched_action=None, p_allied_response=None
        )
        failed = False
    else:
        judge = await judge_geo_rescue(title, text, config=ctx.config)
        failed = judge.failed
    status = STATUS_ERROR if pir_error or failed else STATUS_OK
    judged = (
        judged_rescued(flat_rescued=flat_rescued, pir_rescued=pir_rescued, judge=judge)
        if status == STATUS_OK
        else None
    )
    return replace(
        row,
        pir=pir,
        status=status,
        p_qw=judge.p_watched_action,
        p_qc=judge.p_allied_response,
        p_qd=judge.p_cyber_geo,
        judged=judged,
        model=ctx.model,
        model_digest=ctx.model_digest,
        judge_model=ctx.judge_model,
        judge_model_digest=ctx.judge_model_digest,
        pir_error=pir_error,
        judge_failed=failed,
    )


async def judge_pending(repo: Any, ctx: JudgeContext) -> dict[str, int]:
    """``pending`` を古い順に上限の件数・時間の予算まで判定する。残りは次の回へ持ち越す。

    先に 72 時間を過ぎた ``pending`` を ``expired`` にする。1 行の失敗は ``error`` (例外は外へ出さない)。
    """
    expired = repo.expire_geo_rescue_shadow(older_than_hours=EXPIRE_HOURS)
    per_run = _env_int(PER_RUN_ENV, DEFAULT_PER_RUN)
    deadline = time.monotonic() + _env_float(BUDGET_ENV, DEFAULT_BUDGET_SECONDS)
    todo = repo.list_pending_geo_rescue_shadow(limit=per_run)
    counts = {"pending": len(todo), "ok": 0, "error": 0, "left": 0, "expired": expired}
    for row in todo:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            counts["left"] += 1
            continue
        try:
            done = await asyncio.wait_for(judge_row(row, ctx), timeout=remaining)
        except TimeoutError:
            counts["left"] += 1  # 予算切れ。pending のまま次の回へ
            continue
        except Exception as e:  # noqa: BLE001 — 影の記録の失敗は他の段に影響させない
            _log.warning("geo_rescue_shadow_row_failed", error=type(e).__name__)
            done = replace(row, status=STATUS_ERROR, model=ctx.model, judge_model=ctx.judge_model)
        repo.update_geo_rescue_judgement(done)
        counts["ok" if done.status == STATUS_OK else "error"] += 1
    _log.info("geo_rescue_shadow_judged", **counts)
    return counts


__all__ = [
    "SHADOW_ENV",
    "GeoRescueCandidate",
    "JudgeContext",
    "article_from_row",
    "cue_types",
    "input_sha256",
    "is_geo_rescue_shadow_enabled",
    "judge_pending",
    "judge_row",
    "pending_rows",
]
