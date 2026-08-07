"""切り株記事の全文再取得ジョブ (2026-07-27, A4)。

docs/body_extraction_and_entity_integrity_redesign.md §2.4。

毎時、全文取得に失敗して feed 抜粋 (切り株) で保存された記事、または body NULL の記事を
少量ずつ再取得し、成功したら全文化 + 再エンリッチ (entity replace / 主題再判定 / 分析列更新 /
body_ja 無効化) する。**Discord 再投稿はしない** (web 側 record の silent enrichment)。

設計 (本文自動翻訳ジョブと同型):
- UA 修正 (§2.2) 後、切り株の大半は全文取得できるようになる。高 importance・新しい順に優先。
- 記事単位で all-or-nothing。1 件の失敗はログして続行 (次周期で自然に再試行)。
- 時間予算内で batch を処理。RSS 収集 (:00 台) / scraper (:30) と重ならない slot に置く。
- ジョブ無効化で即停止 (切り株はそのまま残る。手動再取得は scripts/refetch_stumps.py)。
"""

from __future__ import annotations

import time

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

# 1 run の上限件数と時間予算。再取得 (HTTP + trafilatura) + 再エンリッチ (LLM 要約 + 判定) で
# 1 件あたり ~20-40 秒。batch 20 + 予算 12 分で毎時スロットに収める。
_BATCH_LIMIT = 20
_TIME_BUDGET_SECONDS = 12 * 60


async def run_body_refetch_backlog(
    repo: RunHistoryRepository | None = None,
    llm: LLMClient | None = None,
    *,
    batch_limit: int = _BATCH_LIMIT,
    time_budget_seconds: float = _TIME_BUDGET_SECONDS,
) -> dict[str, int]:
    """切り株/未取得記事を全文再取得して再エンリッチする。統計 dict を返す。"""
    from src.config_loader import load_app_config, load_llm_enrichment
    from src.pipeline.dispatch import _load_template
    from src.pipeline.reprocess import reprocess_article_body
    from src.tools.content_extractor import ContentExtractor
    from src.tools.model_tiers import Step, build_llm_for

    repo = repo or RunHistoryRepository()
    cfg = load_app_config()
    if llm is None:
        llm = build_llm_for(Step.ARTICLE_SUMMARY, cfg)
    enrichment = load_llm_enrichment()
    template = _load_template()

    targets = repo.list_articles_needing_refetch(limit=batch_limit)
    started = time.monotonic()
    refetched = 0
    still_stump = 0
    skipped = 0
    skipped_budget = 0
    async with ContentExtractor() as extractor:
        for i, (article_id, url) in enumerate(targets):
            if time.monotonic() - started > time_budget_seconds:
                skipped_budget = len(targets) - i
                break
            try:
                outcome = await reprocess_article_body(
                    repo,
                    article_id,
                    url,
                    extractor=extractor,
                    llm=llm,
                    template=template,
                    enrichment=enrichment,
                )
            except Exception as e:  # noqa: BLE001 — 1 件の失敗でジョブを落とさない
                still_stump += 1
                _log.warning("body_refetch_item_failed", article_id=article_id, error=str(e))
                continue
            if outcome == "refetched_full":
                refetched += 1
            elif outcome == "still_stump":
                still_stump += 1
            else:
                skipped += 1

    stats = {
        "picked": len(targets),
        "refetched_full": refetched,
        "still_stump": still_stump,
        "skipped": skipped,
        "skipped_budget": skipped_budget,
        "elapsed_seconds": int(time.monotonic() - started),
    }
    _log.info("body_refetch_done", **stats)
    return stats
