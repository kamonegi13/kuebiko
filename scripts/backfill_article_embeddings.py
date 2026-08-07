#!/usr/bin/env python3
"""検索改善 A3: embedding 未生成の article を backfill して被覆率を上げる。

semantic / hybrid 検索の vector leg は article_embeddings を読むが、embedding は
ある時期以降のパイプラインでしか生成されておらず、古い記事の多くが未生成 (被覆率
~39%)。本 script は未生成 url を新しい順に embed して被覆率を ~100% に上げる。

設計:
- dedup_seen_urls にあり article_embeddings に無い url を対象 (FK 整合確保)。
- document テキスト = title + LLM要約/本文の先頭 (kind="document" で無印 embed)。
- レート制限 + 中断再開可 (未生成のみ対象なので resume は再実行するだけ)。

Usage:
    docker exec kuebiko /app/.venv/bin/python3 -m scripts.backfill_article_embeddings \\
        --max-articles 2000 --rate-limit-seconds 0.1
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_app_config  # noqa: E402
from src.logging_config import get_logger  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.text_utils import strip_html  # noqa: E402

_log = get_logger(__name__)
_DOC_MAX_CHARS = 1500


def _document_text(title: str, summary: str | None, body: str | None) -> str:
    """embedding 入力 (title + 要約/本文先頭)。"""
    tail = (summary or "").strip() or strip_html(body or "")
    return f"{title}\n\n{tail}".strip()[: _DOC_MAX_CHARS + len(title) + 2]


async def main() -> int:
    parser = argparse.ArgumentParser(description="article embedding backfill (A3)")
    parser.add_argument("--max-articles", type=int, default=2000)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--rate-limit-seconds", type=float, default=0.05)
    args = parser.parse_args()

    config = load_app_config()
    # embedding モデルはティア設定 (DB) が SSoT — production と同じ seam で解決する
    # (旧 config.ollama_embed_model 直読みは model-tier 移行で陳腐化していた)。
    from src.tools.model_tiers import resolve_embedding_model

    model = (resolve_embedding_model() or "").strip()
    if not model:
        _log.error("backfill_aborted", reason="embedding ティア未設定")
        return 2

    from src.tools.embedding_client import OllamaEmbeddingClient

    embedder = OllamaEmbeddingClient(
        base_url=config.ollama_base_url,
        model=model,
        query_prefix=config.ollama_embed_query_prefix,
    )
    repo = RunHistoryRepository()

    before = repo.count_embeddings(model=model)
    _log.info("backfill_start", model=model, existing=before)

    done = 0
    failed = 0
    while done < args.max_articles:
        rows = repo.list_urls_missing_embedding(model=model, limit=args.batch_size)
        if not rows:
            break
        done_before_batch = done
        # url → article (title/summary) を batch 解決
        arts = repo.get_articles_by_urls([url for _h, url, _t, _f in rows])
        for url_hash, url, dedup_title, first_seen in rows:
            if done >= args.max_articles:
                break
            rec = arts.get(url)
            title = (rec.title if rec else "") or dedup_title or url
            summary = rec.summary if rec else None
            body = repo.get_article_body(rec.article_id) if rec else None
            text = _document_text(title, summary, body)
            if not text:
                failed += 1
                continue
            try:
                resp = await embedder.embed(text, kind="document")
            except Exception as e:  # noqa: BLE001
                _log.warning("backfill_embed_failed", url_hash=url_hash[:12], error=str(e))
                failed += 1
                continue
            repo.add_article_embedding(
                url_hash=url_hash,
                url=url,
                vector=list(resp.vector),
                model=model,
                title=title[:200],
                # created_at は now() でなく初出時刻に (dedup 窓の過剰 dedup 防止)
                when=first_seen,
            )
            done += 1
            if done % 50 == 0:
                _log.info("backfill_progress", done=done, failed=failed)
            if args.rate_limit_seconds > 0:
                await asyncio.sleep(args.rate_limit_seconds)
        if done == done_before_batch:
            # 1 batch まるごと失敗 (Ollama 停止/本文なし等)。クエリは毎回先頭から
            # 同じ行を返すため、進捗ゼロのまま回すと無限ループになる (監査 2026-08-01)。
            _log.warning("backfill_stalled_batch", failed=failed)
            break

    after = repo.count_embeddings(model=model)
    _log.info("backfill_complete", embedded=done, failed=failed, total_now=after)
    print(f"done={done} failed={failed} embeddings {before}->{after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
