"""summarizer 判定基準 (rubric) の REST API (層分け WP2)。

``src/prompts/`` (WP1) が持つ DB SSoT + 合成ロジックを Web UI から編集できるように
する 4 endpoint (GET/PUT/preview/test)。**新しい path prefix は作らない** —
``/api/v1/prompts`` 配下に置くことで ``READ_ONLY_GET_DENYLIST`` の前方一致遮断を
自動継承する (公開面に判定基準の詳細を出さない、CLAUDE.md §12 I9)。

保存は ``config_store`` のみ行い、``.j2`` ファイルへは一切書き戻さない (I8)。
LLM dry-run (``/rubric/test``) はプロンプト本文・記事本文・LLM 出力をログに
出さない (CLAUDE.md §4) — ログに残すのはモデル名・所要時間・件数のみ。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from src.config_loader import load_app_config
from src.logging_config import get_logger
from src.pipeline.briefing import MAX_LLM_BODY_CHARS
from src.pipeline.summary import SummaryOutput
from src.prompts.rubric_guide import guide_payload
from src.prompts.rubric_model import (
    RubricIssue,
    RubricValidation,
    SummarizerRubric,
    contract_fields,
    validate_rubric,
)
from src.prompts.rubric_store import (
    CONFIG_KEY,
    build_summarizer_template,
    invalidate_summarizer_cache,
    is_composer_enabled,
    last_fallback_reason,
    load_rubric,
)
from src.prompts.sample_article import SAMPLE_ARTICLE
from src.prompts.summarizer_composer import LEGACY_TEMPLATE_PATH, build_template, compose
from src.storage import config_store
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.model_tiers import Step, build_llm_for
from src.ui.services.rubric_field_stats import MAX_DAYS, MIN_DAYS, field_stats_cached

_log = get_logger(__name__)

prompt_rubric_api = APIRouter(prefix="/api/v1/prompts/summarizer", tags=["prompts"])

# 400 応答の detail に連結するエラーの最大件数 (§5-5)。
_MAX_ERRORS_IN_DETAIL = 10
# preview の composed / rendered_sample の切り詰め上限 (§5-3)。
_PREVIEW_CHAR_CAP = 80_000


def _join_errors(errors: tuple[str, ...]) -> str:
    return "; ".join(errors[:_MAX_ERRORS_IN_DETAIL])


def _no_rubric_error() -> HTTPException:
    return HTTPException(status_code=500, detail="判定基準が見つかりません (seed 未投入)")


def _issue_payload(issue: RubricIssue) -> dict[str, Any]:
    """検証 issue 1 件を JSON 化する (UI がカードへ飛ぶための位置情報つき)。"""
    return {
        "severity": issue.severity,
        "code": issue.code,
        "message": issue.message,
        "field_id": issue.field_id,
        "example_index": issue.example_index,
        "keys": list(issue.keys),
    }


# ---------- GET /rubric ----------


def _contract_payload() -> dict[str, Any]:
    """schema 契約 (24 field) を返す (UI の型/必須/enum バッジ用)。

    宣言状況との突合 (unknown/missing) は preview 応答に一本化する — GET 側にも持つと
    消費者のいない write-only な重複になる (有機的結合監査 2026-07-12 の規約)。
    """
    fields = [
        {
            "name": f.name,
            "json_type": f.json_type,
            "required": f.required,
            "nullable": f.nullable,
            "enum": list(f.enum),
        }
        for f in contract_fields()
    ]
    return {"fields": fields}


def _runtime_payload() -> dict[str, Any]:
    """``build_summarizer_template`` を実際に呼んで判定する (表示と実体を一致させる)。"""
    template = build_summarizer_template(LEGACY_TEMPLATE_PATH)
    history = config_store.list_history(CONFIG_KEY, limit=1)
    version = history[0].version if history else None
    saved_at = history[0].created_at if history else ""
    return {
        "composer_enabled": is_composer_enabled(),
        "active_source": "composed" if template is not None else "legacy_file",
        "fallback_reason": last_fallback_reason(),
        "legacy_path": str(LEGACY_TEMPLATE_PATH),
        "version": version,
        "saved_at": saved_at,
    }


@prompt_rubric_api.get("/rubric")
async def get_rubric() -> dict[str, Any]:
    """現行の判定基準 + schema 契約 + runtime 状態 (合成 or legacy) を返す。"""
    rubric = load_rubric()
    if rubric is None:
        raise _no_rubric_error()
    return {
        "rubric": rubric.model_dump(),
        "contract": _contract_payload(),
        "runtime": _runtime_payload(),
        # フィールドの分類と「効く先」。ラベル自体は rubric_group 語彙が SSoT なので
        # ここでは group id のみを返す (UI に文言表を複製させない)。
        "guide": guide_payload(
            [s.field_id for s in rubric.sections],
            {s.field_id: s.kind for s in rubric.sections},
        ),
    }


# ---------- PUT /rubric ----------


class SaveRubricRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rubric: SummarizerRubric
    note: str = Field(default="", max_length=200)


@prompt_rubric_api.put("/rubric")
async def put_rubric(req: SaveRubricRequest) -> dict[str, Any]:
    """判定基準を検証して DB に版保存する。エラーがあれば 400 で保存を拒否する。"""
    validation: RubricValidation = validate_rubric(req.rubric)
    if not validation.is_valid:
        raise HTTPException(status_code=400, detail=_join_errors(validation.errors))
    version = config_store.save_config(
        CONFIG_KEY,
        req.rubric.model_dump(),
        note=req.note.strip() or "UI 編集",
    )
    invalidate_summarizer_cache()
    _log.info(
        "summarizer_rubric_saved",
        version=version,
        section_count=len(req.rubric.sections),
        warning_count=len(validation.warnings),
    )
    return {
        "saved": True,
        "version": version,
        "warnings": list(validation.warnings),
        "composed_chars": validation.composed_chars,
        "legacy_chars": validation.legacy_chars,
    }


# ---------- POST /rubric/preview ----------


class PreviewRubricRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rubric: SummarizerRubric | None = None


@prompt_rubric_api.post("/rubric/preview")
async def preview_rubric(req: PreviewRubricRequest) -> dict[str, Any]:
    """検証結果 + 合成原文 + サンプル記事でのレンダリング結果を返す (常に 200)。

    ``rubric`` 省略時は現行保存値を使う。レンダリング失敗 (I6: 保存前検証の最後の砦) は
    例外を投げず ``errors`` に追加して ``valid: false`` として返す。
    """
    rubric = req.rubric or load_rubric()
    if rubric is None:
        raise _no_rubric_error()
    # render 検証は validate_rubric 内 (_check_render) に一本化済み — ここでの render は
    # rendered_sample の取得のみが目的で、失敗は errors に計上済みのため握って空にする。
    validation = validate_rubric(rubric)
    errors = list(validation.errors)
    composed = compose(rubric)
    rendered_sample = ""
    try:
        template = build_template(rubric, path=LEGACY_TEMPLATE_PATH)
        rendered_sample = template.render(
            article=SAMPLE_ARTICLE,
            body=SAMPLE_ARTICLE.body_text or "",
        )
    except Exception:  # noqa: BLE001, S110 — 失敗理由は validation.errors が持っている
        pass
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": list(validation.warnings),
        # errors/warnings と同内容を「どのカードの問題か」つきで返す (UI の局在化用)。
        # 文字列列は後方互換のため残す (I-3)。
        "issues": [_issue_payload(i) for i in validation.issues],
        "unknown_fields": list(validation.unknown_fields),
        "missing_fields": list(validation.missing_fields),
        "composed": composed[:_PREVIEW_CHAR_CAP],
        "composed_chars": validation.composed_chars,
        "legacy_chars": validation.legacy_chars,
        "rendered_sample": rendered_sample[:_PREVIEW_CHAR_CAP],
    }


# ---------- GET /rubric/field-stats ----------


# ⚠ **同期 def で定義する** (async にしない)。数十本の集計クエリが event loop を占有すると
# UI 全体が固まる (PIR 一覧 18秒→2.5秒 の教訓)。FastAPI が threadpool に逃がす。
@prompt_rubric_api.get("/rubric/field-stats")
def field_stats(days: int = Query(30, ge=MIN_DAYS, le=MAX_DAYS)) -> dict[str, Any]:
    """直近 ``days`` 日の実出力統計 (判定基準を直す前に現状を見るための接地)。"""
    return field_stats_cached(days)


# ---------- POST /rubric/test (LLM dry-run) ----------


class TestRubricRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rubric: SummarizerRubric | None = None
    article_id: str | None = None


def _resolve_test_article(request: Request, article_id: str | None) -> Article | None:
    """テスト対象記事を組み立てる。``article_id`` 省略時は固定サンプル記事。"""
    if not article_id:
        return SAMPLE_ARTICLE
    repo: RunHistoryRepository = request.app.state.repo
    record = repo.get_article(article_id)
    if record is None:
        return None
    body = repo.get_article_body(article_id) or record.summary or ""
    return Article(
        id=record.article_id,
        title=record.title,
        url=record.url,
        summary_html="",
        published=record.published_at or record.created_at,
        feed_title=record.feed_title or "",
        feed_url=record.feed_url or "",
        body_text=body,
    )


def _empty_field_names(output: SummaryOutput) -> list[str]:
    """値が ``None``/``""``/``[]``/``{}`` のフィールド名 (末尾フィールド枯死の可視化用)。"""
    return sorted(k for k, v in output.model_dump().items() if v in (None, "", [], {}))


@prompt_rubric_api.post("/rubric/test")
async def test_rubric(request: Request, req: TestRubricRequest) -> dict[str, Any]:
    """判定基準を実際に LLM へ投げて 1 件試す (dry-run)。LLM 失敗は 200 + ok:false で返す。"""
    rubric = req.rubric or load_rubric()
    if rubric is None:
        raise _no_rubric_error()
    validation = validate_rubric(rubric)
    if not validation.is_valid:
        raise HTTPException(status_code=400, detail=_join_errors(validation.errors))

    article = _resolve_test_article(request, req.article_id)
    if article is None:
        raise HTTPException(status_code=404, detail=f"記事が見つかりません: {req.article_id}")

    llm_body = (article.body_text or "")[:MAX_LLM_BODY_CHARS]
    try:
        template = build_template(rubric, path=LEGACY_TEMPLATE_PATH)
        prompt = template.render(article=article, body=llm_body)
    except Exception as e:  # noqa: BLE001 — 合成/レンダリング失敗は LLM 呼出前に 400 で返す
        raise HTTPException(
            status_code=400,
            detail=f"合成テンプレートの構築に失敗しました: {type(e).__name__}",
        ) from e

    llm = build_llm_for(Step.ARTICLE_SUMMARY, load_app_config())

    start = time.monotonic()
    try:
        output = await llm.generate_structured(prompt, schema=SummaryOutput, think=False)
    except Exception as e:  # noqa: BLE001 — LLM 失敗は 200 + ok:false (§5-4)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log.warning(
            "summarizer_rubric_test_failed",
            model=llm.model,
            elapsed_ms=elapsed_ms,
            error_type=type(e).__name__,
        )
        return {
            "ok": False,
            "model": llm.model,
            "elapsed_ms": elapsed_ms,
            "prompt_chars": len(prompt),
            "output": None,
            "empty_fields": [],
            "error": f"{type(e).__name__}: {e}",
        }

    elapsed_ms = int((time.monotonic() - start) * 1000)
    empty_fields = _empty_field_names(output)
    _log.info(
        "summarizer_rubric_test_ok",
        model=llm.model,
        elapsed_ms=elapsed_ms,
        empty_field_count=len(empty_fields),
    )
    return {
        "ok": True,
        "model": llm.model,
        "elapsed_ms": elapsed_ms,
        "prompt_chars": len(prompt),
        "output": output.model_dump(),
        "empty_fields": empty_fields,
        "error": None,
    }
