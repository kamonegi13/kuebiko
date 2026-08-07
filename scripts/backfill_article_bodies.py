#!/usr/bin/env python3
"""Phase Diamond L2-3: 既存 article の本文を trafilatura で再取得して articles.body へ保存。

設計方針:
- articles.body IS NULL の record を新しい順に処理 (古い URL ほど死亡率高い → 後回し)
- レート制限: --rate-limit-seconds (default 1.5s) で外部サイトに優しく
- 失敗時は body=NULL のまま (再 run で resume 可)、body_fetched_at は更新
- 中断・再開可能 (body_fetched_at IS NULL を skip 条件にしない、body IS NULL で resume)
- 進捗ログ: 10 件ごとに structured log + 統計

Usage:
    docker exec kuebiko /app/.venv/bin/python3 -m scripts.backfill_article_bodies \\
        --max-articles 200 --rate-limit-seconds 1.0
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

# repo root を sys.path に
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.logging_config import get_logger  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.content_extractor import ContentExtractor  # noqa: E402

_log = get_logger(__name__)


async def main() -> int:
    parser = argparse.ArgumentParser(description="article 本文 backfill")
    parser.add_argument("--max-articles", type=int, default=2000, help="処理上限件数")
    parser.add_argument("--batch-size", type=int, default=50, help="DB 1 query で取る件数")
    parser.add_argument(
        "--rate-limit-seconds",
        type=float,
        default=1.0,
        help="1 article fetch 後の wait (rate-limit)",
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=90,
        help="現在から N 日以内の article を対象 (古い URL は死亡率高)",
    )
    args = parser.parse_args()

    repo = RunHistoryRepository()
    since_iso = (datetime.now(UTC) - timedelta(days=args.max_days)).isoformat()

    total_processed = 0
    total_ok = 0
    total_fail = 0
    start_ts = time.monotonic()

    async with ContentExtractor() as extractor:
        while total_processed < args.max_articles:
            remaining = args.max_articles - total_processed
            batch_n = min(args.batch_size, remaining)
            targets = repo.list_articles_missing_body(limit=batch_n, since_iso=since_iso)
            if not targets:
                _log.info("backfill_complete_no_more", total_processed=total_processed)
                break

            for article_id, url in targets:
                total_processed += 1
                try:
                    result = await extractor.extract(url)
                except Exception as e:  # noqa: BLE001
                    _log.warning(
                        "backfill_extract_exception",
                        article_id=article_id,
                        url=url[:80],
                        error=str(e),
                    )
                    total_fail += 1
                    continue
                if not result.success or not (result.text or "").strip():
                    _log.debug(
                        "backfill_extract_empty",
                        article_id=article_id,
                        url=url[:80],
                        failure=result.failure_reason,
                    )
                    total_fail += 1
                    continue
                body = result.text[:20_000]
                # 本文完全性 (2026-07-27): backfill も body_source を明示 (seam 不変条件)。
                body_source = (
                    "playwright_extract"
                    if result.extraction_method == "playwright+trafilatura"
                    else "full_extract"
                )
                try:
                    updated = repo.update_article_body(article_id, body, source=body_source)
                    if updated:
                        total_ok += 1
                except Exception as e:  # noqa: BLE001
                    _log.warning(
                        "backfill_db_update_failed",
                        article_id=article_id,
                        error=str(e),
                    )
                    total_fail += 1
                    continue
                if total_processed % 10 == 0:
                    elapsed = time.monotonic() - start_ts
                    _log.info(
                        "backfill_progress",
                        processed=total_processed,
                        ok=total_ok,
                        fail=total_fail,
                        rate_per_min=int(60 * total_processed / max(elapsed, 1)),
                    )
                await asyncio.sleep(args.rate_limit_seconds)

    elapsed = time.monotonic() - start_ts
    _log.info(
        "backfill_done",
        processed=total_processed,
        ok=total_ok,
        fail=total_fail,
        elapsed_seconds=int(elapsed),
        success_rate_pct=int(100 * total_ok / max(total_processed, 1)),
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
