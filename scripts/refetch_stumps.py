"""切り株記事の全文再取得を手動加速する (2026-07-27, A4)。

毎時ジョブ (body-refetch-backlog) と同じ reprocess_article_body を、大きめの batch で
連続実行する。UA 修正後の遡及救済 (既存の切り株 ~1,200 件) を一気に流すのに使う。

使い方 (コンテナ内で):
  docker exec kuebiko uv run python -m scripts.refetch_stumps --max 500 --batch 20

Discord 再投稿はしない (web 側 record の silent enrichment)。
"""

from __future__ import annotations

import argparse
import asyncio
import time

from src.config_loader import load_app_config, load_llm_enrichment
from src.logging_config import configure_logging, get_logger
from src.pipeline.dispatch import _load_template
from src.pipeline.reprocess import reprocess_article_body
from src.storage.run_history import RunHistoryRepository
from src.tools.content_extractor import ContentExtractor
from src.tools.model_tiers import Step, build_llm_for

_log = get_logger(__name__)


async def _run(max_articles: int, batch_size: int, rate_limit_seconds: float) -> None:
    repo = RunHistoryRepository()
    cfg = load_app_config()
    llm = build_llm_for(Step.ARTICLE_SUMMARY, cfg)
    enrichment = load_llm_enrichment()
    template = _load_template()

    total = 0
    refetched = 0
    still_stump = 0
    start = time.monotonic()
    async with ContentExtractor() as extractor:
        while total < max_articles:
            remaining = max_articles - total
            targets = repo.list_articles_needing_refetch(limit=min(batch_size, remaining))
            if not targets:
                _log.info("refetch_complete_no_more", total=total)
                break
            for article_id, url in targets:
                total += 1
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
                except Exception as e:  # noqa: BLE001
                    still_stump += 1
                    _log.warning("refetch_item_failed", article_id=article_id, error=str(e))
                    continue
                if outcome == "refetched_full":
                    refetched += 1
                elif outcome == "still_stump":
                    still_stump += 1
                if total % 10 == 0:
                    elapsed = time.monotonic() - start
                    _log.info(
                        "refetch_progress",
                        total=total,
                        refetched=refetched,
                        still_stump=still_stump,
                        rate_per_min=int(60 * total / max(elapsed, 1)),
                    )
                await asyncio.sleep(rate_limit_seconds)
            # list_articles_needing_refetch は成功で母集団が縮むため、targets が全て
            # still_stump だと同じ集合を無限ループする恐れ → 前進しなければ打ち切る。
            if refetched == 0 and still_stump >= total:
                _log.info("refetch_no_progress_stop", total=total)
                break

    _log.info("refetch_done", total=total, refetched=refetched, still_stump=still_stump)


def main() -> None:
    configure_logging()
    ap = argparse.ArgumentParser(description="切り株記事の全文再取得を手動加速する")
    ap.add_argument("--max", type=int, default=200, dest="max_articles")
    ap.add_argument("--batch", type=int, default=20, dest="batch_size")
    ap.add_argument("--rate-limit", type=float, default=1.0, dest="rate_limit_seconds")
    args = ap.parse_args()
    asyncio.run(_run(args.max_articles, args.batch_size, args.rate_limit_seconds))


if __name__ == "__main__":
    main()
