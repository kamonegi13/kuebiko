"""記事詳細の単記事操作 API (2026-07-25)。

- POST /api/v1/articles/{id}/translate — 本文のオンデマンド日本語訳。
  body_ja にキャッシュし、2 回目以降は DB から即返す。readonly instance でも
  許可 (app.py の READ_ONLY allowlist — モバイル閲覧が翻訳の主用途のため。
  write は body_ja 1 列の upsert のみで他の write 遮断は維持)。
- GET /api/v1/articles/{id}/stix — 単記事の STIX 2.1 bundle export。
  IoC は body への extract_iocs 再実行 + article_entities の両方から合流する
  (entities には email/IPv6 等が落ちない取りこぼしがあり、body は 90 日
  retention で消えるため、双方を補完し合う)。GET なので readonly でも使える。

route 注意: articles_feed の ``GET /articles/{article_id:path}`` が貪欲 match
するため、本 router は app.py で articles_feed_api より **先に** 登録すること。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from src.config_loader import load_app_config
from src.cti.actor_normalizer import ActorAlias, load_actor_aliases
from src.cti.body_translator import is_probably_japanese, translate_body_resumable
from src.cti.ioc_extractor import extract_iocs
from src.cti.stix_exporter import to_bundle
from src.cti.stix_from_briefing import make_attachment_filename
from src.cti.subject_gate import passes_subject_gate
from src.logging_config import get_logger
from src.tools.llm_client import LLMError, LLMForbiddenModelError
from src.tools.model_tiers import Step, build_llm_for

_log = get_logger(__name__)

article_ops_api = APIRouter(prefix="/api/v1/articles", tags=["article-ops"])

# 翻訳 1 リクエストの時間上限。超えたらチャンク境界で中断して partial を返し、
# frontend が自動で続きを要求する (100k 字 ≈ 20 チャンクの一枚岩リクエスト化を防ぐ)。
_TRANSLATE_REQUEST_DEADLINE_SECONDS = 120.0


@article_ops_api.post("/{article_id:path}/translate")
async def translate_article(request: Request, article_id: str) -> dict[str, Any]:
    """本文を日本語に全訳して返す (body_ja キャッシュ、押した時だけ翻訳)。"""
    aid = article_id.strip()
    if not aid:
        raise HTTPException(status_code=400, detail="article_id は必須")

    repo = request.app.state.repo
    if repo.get_article(aid) is None:
        raise HTTPException(status_code=404, detail=f"article が見つかりません: {aid}")

    cached = repo.get_article_body_ja(aid)
    if cached:
        return {"body_ja": cached, "cached": True}

    body = repo.get_article_body(aid)
    if not body or not body.strip():
        raise HTTPException(
            status_code=404,
            detail="本文が保存されていません (90 日 retention で削除済みの可能性)",
        )
    if is_probably_japanese(body):
        # 日本語原文の「翻訳」は LLM が逆方向 (日→英) に訳して壊れるため拒否する。
        raise HTTPException(status_code=400, detail="原文が既に日本語です (翻訳不要)")

    try:
        llm = build_llm_for(Step.ARTICLE_TRANSLATE, load_app_config())
    except LLMForbiddenModelError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 — LLM 構築失敗はユーザー向けに 503
        _log.error("article_translate_llm_init_failed", error=str(e))
        raise HTTPException(status_code=503, detail="LLM を初期化できません") from e

    try:
        progress = await translate_body_resumable(
            llm,
            body,
            article_id=aid,
            store=repo,
            deadline_seconds=_TRANSLATE_REQUEST_DEADLINE_SECONDS,
        )
    except LLMError as e:
        # 訳し終えたチャンクは body_ja_chunks に確定済み。再試行は続きから再開する。
        _log.error("article_translate_failed", article_id=aid, error=str(e))
        raise HTTPException(
            status_code=502,
            detail="翻訳に失敗しました。再試行すると続きから再開します",
        ) from e

    if progress.text is None:
        # 時間上限で中断 (長文)。frontend は partial を見て自動で続きを要求する。
        return {
            "body_ja": None,
            "cached": False,
            "partial": True,
            "partial_text": progress.partial_text,
            "done_chunks": progress.done_chunks,
            "total_chunks": progress.total_chunks,
        }

    repo.update_article_body_ja(aid, progress.text)
    repo.clear_body_ja_chunks(aid)
    return {
        "body_ja": progress.text,
        "cached": False,
        "partial": False,
        "done_chunks": progress.done_chunks,
        "total_chunks": progress.total_chunks,
    }


@article_ops_api.get("/{article_id:path}/stix")
async def article_stix(request: Request, article_id: str) -> Response:
    """単記事の STIX 2.1 bundle を download 形式で返す。"""
    aid = article_id.strip()
    if not aid:
        raise HTTPException(status_code=400, detail="article_id は必須")

    repo = request.app.state.repo
    a = repo.get_article(aid)
    if a is None:
        raise HTTPException(status_code=404, detail=f"article が見つかりません: {aid}")

    body = repo.get_article_body(aid) or ""
    ent_raw: dict[str, dict[str, int]] = repo.count_entities_for_articles([aid])

    # IoC 抽出コーパス: title + body + 保存済み entity 値 (flat 文字列)。
    # extract_iocs が refang + 種別判定を 1 パスで行うため、単純連結で合流できる。
    entity_values = [v for values in ent_raw.values() for v in values]
    corpus = "\n".join([a.title or "", body, *entity_values])
    extracted = extract_iocs(corpus)

    actors = _resolve_article_actors(
        ent_raw,
        corpus,
        subject_ids_csv=a.subject_actor_ids,
        subject_source=a.subject_actor_source,
    )
    description = f"{a.title} ({a.url})" if a.url else (a.title or aid)
    bundle = to_bundle(
        extracted,
        actors,
        description=description,
        socio_political_intent=a.socio_political_intent,
    )
    payload = json.dumps(bundle, ensure_ascii=False, indent=2)
    filename = make_attachment_filename(importance=a.importance or "na", article_id=aid)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _resolve_article_actors(
    ent_raw: dict[str, dict[str, int]],
    corpus: str,
    *,
    subject_ids_csv: str | None = None,
    subject_source: str | None = None,
) -> list[ActorAlias]:
    """entity の actor id を辞書解決する (STIX threat-actor 化)。

    subject-gate (2026-07-29): STIX は外部連携 (TIP 等) に流れるため誤帰属流出が最も
    高コスト。評価済み記事は主題 actor のみ、legacy 記事は mention をそのまま出す。
    評価済みで主題なしの記事は空 (本文走査 fallback もしない — 誤帰属を外に出さない)。
    """
    registry = load_actor_aliases()
    out: list[ActorAlias] = []
    seen: set[str] = set()
    for actor_id in ent_raw.get("actor", {}):
        if not passes_subject_gate(
            mention_id=actor_id, subject_ids_csv=subject_ids_csv, subject_source=subject_source
        ):
            continue
        actor = registry.by_id(actor_id)
        if actor is not None and actor.id not in seen:
            seen.add(actor.id)
            out.append(actor)
    if out:
        return out
    # entity が空/全て gate 落ち。legacy 行のみ本文走査 fallback (評価済みは空を維持)。
    if not subject_source:
        return registry.find_all(corpus)
    return out
