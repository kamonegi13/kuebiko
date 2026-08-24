"""埋込の取りこぼしを毎時埋めるジョブ。

**なぜ要るか (2026-08-24)**: 埋込は意味的 dedup の判定時にしか生成されず、保存は
「投稿確定した記事」の経路でしか行われない。この経路を通らない記事は埋込を
永久に持たず、**事象ニュースの群化 (cos >= 0.70) に一度も参加できない**。

実測 (直近 14 日、3,857 記事):

| 分類 | 件数 | 埋込 |
|---|---|---|
| posted (通常記事) | 2,734 | 2,730 あり |
| x.com (Grok) | 445 | **0** |
| collected 系 (被害者レコード) | 479 | ほぼ 0 |

Grok は 1 ツイート = sub-article に展開されるが、既読化は **親レポートの URL** で
行われるため、ツイート URL は ``dedup_seen_urls`` に 1 件も入らない
(実測 x.com: articles 906 / dedup 0)。既存の backfill script は dedup_seen_urls を
起点にするため、この層を構造的に拾えない。

ここでは **articles を起点** にし、必要なら ``mark_url_seen`` を先に打ってから
埋込を作る。時刻は記事の取込時刻を刻む — now() にすると過去記事が一斉に
「最近」化し、dedup 窓が歴史全体を見て過剰 dedup する。

``EMBEDDING_BACKFILL=0`` で停止できる。
"""

from __future__ import annotations

import os
import time

from src.config_loader import load_app_config
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.tools.text_utils import strip_html
from src.tools.url_normalizer import url_hash

_log = get_logger(__name__)

_FLAG = "EMBEDDING_BACKFILL"
# 1 回の実行で埋める上限。毎時 30-70 件の取りこぼしに対し十分な余裕を持たせつつ、
# 他ジョブとの Ollama 競合が長引かないように抑える。
_MAX_PER_RUN = 300
_DOC_MAX_CHARS = 1500


def _document_text(title: str, summary: str | None, body: str | None) -> str:
    """埋込入力 (タイトル + 要約/本文の先頭)。既存 backfill script と同一の組み立て。"""
    tail = (summary or "").strip() or strip_html(body or "")
    return f"{title}\n\n{tail}".strip()[: _DOC_MAX_CHARS + len(title) + 2]


async def run_embedding_backfill() -> dict[str, object]:
    """埋込の無い記事を埋める。戻り値は実行履歴に載せる要約。"""
    if os.environ.get(_FLAG, "1") == "0":
        _log.info("embedding_backfill_disabled")
        return {"skipped": "flag_off"}

    from src.tools.model_tiers import resolve_embedding_model

    model = (resolve_embedding_model() or "").strip()
    if not model:
        _log.warning("embedding_backfill_skipped", reason="embedding ティア未設定")
        return {"skipped": "no_embedding_model"}

    started = time.monotonic()
    repo = RunHistoryRepository()
    rows = repo.list_articles_missing_embedding(model=model, limit=_MAX_PER_RUN)
    if not rows:
        return {"embedded": 0, "elapsed_seconds": 0.0}

    from src.tools.embedding_client import OllamaEmbeddingClient

    config = load_app_config()
    embedder = OllamaEmbeddingClient(
        base_url=config.ollama_base_url,
        model=model,
        query_prefix=config.ollama_embed_query_prefix,
    )

    # 正規化後 hash が既に埋込を持つ記事は対象から外す。fragment (#...) を落とす
    # 正規化により複数記事が同一 hash に潰れることがあり、URL 一致では永久に
    # 見つからないため、除外しないと毎回同じ記事を埋め直し続ける。
    taken = repo.existing_embedding_url_hashes([url_hash(u) for _a, u, _t, _w in rows], model=model)

    embedded = failed = skipped_empty = skipped_alias = 0
    for article_id, url, title, when in rows:
        if url_hash(url) in taken:
            skipped_alias += 1
            continue
        record = repo.get_article(article_id)
        body = repo.get_article_body(article_id)
        text = _document_text(title, record.summary if record else None, body)
        if len(text.strip()) < 20:
            # 本文も要約も無い行 (取得失敗等)。埋めても群化の材料にならない。
            skipped_empty += 1
            continue
        try:
            resp = await embedder.embed(text, kind="document")
        except Exception as exc:  # noqa: BLE001 — 1 件の失敗で全体を止めない
            _log.warning("embedding_backfill_failed", article_id=article_id, error=str(exc))
            failed += 1
            continue
        h = url_hash(url)
        # FK (article_embeddings.url_hash → dedup_seen_urls.url_hash) を満たすため先に既読化。
        # 既に処理済みの記事なので「既読」は事実であり、再収集の抑止も正しい挙動。
        repo.mark_url_seen(url_hash=h, url=url, article_id=article_id, title=title, when=when)
        repo.add_article_embedding(
            url_hash=h,
            url=url,
            vector=list(resp.vector),
            model=model,
            title=title[:200],
            when=when,
        )
        embedded += 1
        taken.add(h)

    elapsed = round(time.monotonic() - started, 1)
    _log.info(
        "embedding_backfill_done",
        embedded=embedded,
        failed=failed,
        skipped_empty=skipped_empty,
        elapsed_seconds=elapsed,
    )
    return {
        "embedded": embedded,
        "failed": failed,
        "skipped_empty": skipped_empty,
        "remaining_hint": len(rows) == _MAX_PER_RUN,
        "elapsed_seconds": elapsed,
    }


__all__ = ["run_embedding_backfill"]
