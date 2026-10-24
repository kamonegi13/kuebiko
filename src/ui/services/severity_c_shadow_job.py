# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""深刻さ C の影の記録を毎時付ける (深刻度の軸の段 ``severity_axes_job`` の続き、2026-10-22)。

直近 72 時間の配信済みの記事のうち未記録のものに、(1) A の決まりごと (``derive_severity``) を導き、
(2) A が深刻さを付けない記事 (非サイバー・派生・まとめ) は問いを当てずに理由だけ記録し、
(3) それ以外は fast ティアに 9 問 (``severity_c.QUESTIONS``) を当てて RF の P(S3) を記録する。
**配信・重要度は一切変えない** (``severity_c_shadow`` への記録のみ)。

時間: 1 記事 9 問 ≈ 3.8 秒。段の上限 (5 分) に収めるため、問いを当てる時間に予算
(``SEVERITY_C_SHADOW_BUDGET_S``、既定 150 秒) を置き、超えた分は次の時間へ持ち越す
(新しい記事から処理する)。外す記事は GPU を使わないので予算に数えない。

停止: ``SEVERITY_C_SHADOW=0``。モデル (``config/models/severity_s3_model.json``) が無ければ何もしない。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from src.cti.importance_v2 import build_inputs, derive_severity
from src.cti.severity_c import (
    QUESTION_IDS,
    ArticleMeta,
    SeverityCModel,
    load_severity_c_model,
    probe_questions,
    scope_skip_reason,
    shadow_enabled,
)
from src.storage.repo_severity_c_shadow import SeverityCShadowRow

_log = structlog.get_logger(__name__)

BUDGET_ENV = "SEVERITY_C_SHADOW_BUDGET_S"
DEFAULT_BUDGET_S = 150.0
#: 対象の窓 (軸の段と同じ。止まっていた時間の取りこぼしを拾う)
LOOKBACK_HOURS = 72
#: 1 回に読む未記録の記事の上限 (外す記事は軽いので多めに読む)
PENDING_LIMIT = 300
#: 欠測の問いがこれを超えたら記録しない (Ollama 不調。次の時間にやり直す)
MAX_MISSING_QUESTIONS = 4
#: 連続でこの件数、全問が欠測なら段を打ち切る (接続不能の無駄打ちを避ける)
MAX_CONSECUTIVE_FAILURES = 3

_SQL = """
SELECT article_id, title, summary, body, published_at, category, article_type,
       victim_country_iso, victim_sector_canonical FROM (
    SELECT a.article_id, a.title, a.summary, a.body, a.published_at, a.category,
           a.article_type, a.victim_country_iso, a.victim_sector_canonical,
           ROW_NUMBER() OVER (PARTITION BY a.article_id ORDER BY a.created_at DESC) AS rn
    FROM articles a
    WHERE a.status = 'posted' AND a.created_at >= ?
      AND NOT EXISTS (SELECT 1 FROM severity_c_shadow s WHERE s.article_id = a.article_id)
) t WHERE rn = 1
ORDER BY article_id
LIMIT ?
"""

ProbeFn = Callable[[str, str | None, str], Awaitable[dict[str, float | None]]]


def shadow_budget_s() -> float:
    raw = os.environ.get(BUDGET_ENV, "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_BUDGET_S
    return value if value > 0 else DEFAULT_BUDGET_S


def pending_rows(repo: Any, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """未記録の配信済み記事 (同一 id の重複行は最新 1 件)。新しい公開順に近づけるため公開日の新しい順。"""
    since = ((now or datetime.now(UTC)) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の接続 seam 共有
        rows = conn.execute(_SQL, (since, PENDING_LIMIT)).fetchall()
    out = [dict(r) for r in rows]
    return sorted(out, key=lambda r: str(r.get("published_at") or ""), reverse=True)


def _materials(repo: Any, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from src.tools.kev_client import get_kev_cve_set
    from src.tools.nvd_client import max_cvss

    ids = [str(r["article_id"]) for r in rows]
    ents = repo._importance_entities(ids)  # noqa: SLF001
    axes = repo.get_severity_axes(ids)
    kinds = repo.get_article_kinds(ids)
    kev = get_kev_cve_set()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        aid = str(r["article_id"])
        e = ents.get(aid, {})
        cves = sorted(e.get("cve", set()))
        kind = str(kinds.get(aid) or "")
        inp = build_inputs(
            category=str(r["category"] or ""),
            article_type=str(r["article_type"] or ""),
            axes=axes.get(aid, {}),
            cves=cves,
            kev=kev,
            year=datetime.now(UTC).year,
            victim_country=str(r["victim_country_iso"] or ""),
            involved_countries=e.get("involved_country", set()),
            mentioned_countries=e.get("mentioned_country", set()),
            sir_ids=e.get("pir", set()),
            text=f"{r['summary'] or ''}\n{r['body'] or ''}",
            stored_max_cvss=max_cvss(cves),
            title=str(r["title"] or ""),
            event_kind=kind,
            summary=str(r["summary"] or ""),
            victim_sector=str(r.get("victim_sector_canonical") or ""),
        )
        sev, basis = derive_severity(inp)
        meta = ArticleMeta(
            category=inp.category,
            article_type=inp.article_type,
            event_kind=kind,
            on_kev=inp.on_kev,
            max_cvss=inp.max_cvss,
            loss_usd=inp.loss_usd,
            n_cves=len(cves),
        )
        out[aid] = {"severity": sev, "basis": basis, "meta": meta}
    return out


def _scored_row(
    aid: str,
    model: SeverityCModel,
    probe_model: str,
    probs: dict[str, float | None],
    material: dict[str, Any],
) -> SeverityCShadowRow:
    p = model.probability(probs, material["meta"])
    return SeverityCShadowRow(
        article_id=aid,
        p_s3=p,
        would_s3=p >= model.threshold,
        threshold=model.threshold,
        probe_model=probe_model,
        trained_model=model.probe_model,
        model_match=probe_model == model.probe_model,
        probs_json=json.dumps(probs, ensure_ascii=False),
        a_severity=material["severity"],
        a_basis=material["basis"],
    )


async def run_shadow(
    repo: Any,
    *,
    probe: ProbeFn,
    probe_model: str,
    model: SeverityCModel,
    budget_s: float,
    now: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """未記録の記事を処理して記録する (部品注入版。テストはここを直接呼ぶ)。"""
    rows = pending_rows(repo, now=now)
    if not rows:
        return {"pending": 0, "probed": 0, "skipped": 0, "failed": 0}
    material = _materials(repo, rows)
    started, probed, skipped, failed, streak = clock(), 0, 0, 0, 0
    for r in rows:
        aid = str(r["article_id"])
        m = material[aid]
        reason = scope_skip_reason(m["basis"], m["meta"].event_kind)
        if reason is not None:
            repo.record_severity_c_shadow(
                SeverityCShadowRow(
                    article_id=aid, skip_reason=reason, a_severity=m["severity"], a_basis=m["basis"]
                )
            )
            skipped += 1
            continue
        if clock() - started >= budget_s or streak >= MAX_CONSECUTIVE_FAILURES:
            continue  # 問いを当てる枠が尽きた。次の時間に持ち越す (外す記事は続けて処理する)
        body = str(r["body"] or "") or str(r["summary"] or "")
        published = str(r["published_at"] or "")[:10] or None
        probs = await probe(str(r["title"] or ""), published, body)
        missing = sum(1 for q in QUESTION_IDS if probs.get(q) is None)
        if missing > MAX_MISSING_QUESTIONS:
            failed += 1
            streak = streak + 1 if missing == len(QUESTION_IDS) else 0
            continue
        streak = 0
        repo.record_severity_c_shadow(_scored_row(aid, model, probe_model, probs, m))
        probed += 1
    return {
        "pending": len(rows),
        "probed": probed,
        "skipped": skipped,
        "failed": failed,
        "seconds": round(clock() - started, 1),
    }


async def run_severity_c_shadow(repo: Any) -> dict[str, Any]:
    """毎時の段の入口 (軸の段の後に呼ぶ)。失敗しても軸の段は成功のまま。"""
    if not shadow_enabled():
        return {"skipped": "flag_off"}
    model = load_severity_c_model()
    if model is None:
        return {"skipped": "no_model"}
    import ollama

    from src.config_loader import load_app_config
    from src.tools.model_tiers import STEP_REGISTRY, Step, is_external_model, resolve_step_model

    config = load_app_config()
    probe_model = resolve_step_model(Step.SEVERITY_AXES)
    if is_external_model(probe_model):
        return {"skipped": "external_model"}  # logprob を読めない
    client = ollama.AsyncClient(
        host=config.ollama_base_url, timeout=STEP_REGISTRY[Step.SEVERITY_AXES].timeout_seconds
    )

    async def _probe(title: str, published: str | None, body: str) -> dict[str, float | None]:
        return await probe_questions(
            title=title, published=published, body=body, config=config, client=client
        )

    try:
        summary = await run_shadow(
            repo,
            probe=_probe,
            probe_model=probe_model,
            model=model,
            budget_s=shadow_budget_s(),
        )
    except Exception as e:  # noqa: BLE001 — 記録のみの段。本処理を止めない
        _log.warning("severity_c_shadow_failed", error=type(e).__name__, detail=str(e)[:200])
        return {"error": type(e).__name__}
    finally:
        await client.close()  # type: ignore[no-untyped-call]
    _log.info("severity_c_shadow_done", **summary)
    return summary
